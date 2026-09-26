# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.2] - 2026-09-26

### Added

- `client_order_id` contract (#13): `create_order(..., client_order_id=...)` and
  `fetch_order(order_id, symbol, *, client_order_id=...)` take the caller's
  idempotency key on the base signature and the sync stubs.
  - Binance: sent as `newClientOrderId` on create and looked up with
    `origClientOrderId` on fetch; the order parser echoes `clientOrderId`.
  - Bitget: `clientOid` on create and fetch; echoed on create and by the mix
    parser.
  - OKX: create is unchanged (`clOrdId`); `fetch_order` now looks an order up by
    `clOrdId`.

### Changed

- `fetch_order`'s `order_id` is `str | None`; pass `client_order_id` instead of
  it on Binance, Bitget, and OKX.
- Upbit, Bithumb, Bybit, and Korbit raise `NotSupportedError` when
  `client_order_id` is given, and their `fetch_order` requires an exchange
  order ID.

## [0.4.1] - 2026-09-22

### Removed

- Removed: `CONSERVATIVE_RATE_LIMIT`, `CONSERVATIVE_MAX_INFLIGHT` (breaking).
  No call sites remain in quantus-mono or trader.

### Changed

- Rate-limit defaults are 80% of each venue's published cap (floored).
  OKX orders stay at the previous 5/s cap. Bitget orders stay at 5/s.
  Confirmed venues allow 4 in-flight calls. Bybit uses the same
  pre-flight limiter. `fetch_candles_history` 429 backoff is unchanged.

## [0.4.0] - 2026-09-22

### Added

- `fetch_candles(..., closed_only=True)` drops a bar that has not ended
  (`timestamp + timeframe_ms <= now_ms`). `Candle.timestamp` stays the bar
  open on every adapter.
- `Market.listed_at` — venue listing time when the exchange publishes one
  (OKX `listTime`, Binance linear `onboardDate`, Bitget `launchTime` /
  `onlineTime`, Bybit `launchTime`). Otherwise `None`.
- `fetch_candles_history(symbol, timeframe, since, until=None)` async
  generator and `fetch_candles_history_sync` list twin. Pages at
  `CANDLE_VENUES` limits, stops on an empty page, and when `until` is omitted
  stops at the last closed bar. `RateLimitError` backs off on `retry_after`
  (or 1, 2, 4, 8, 16 seconds) at most five times. Other methods still do not
  retry.
- `CANDLE_VENUES` in `pycex.constants` is the page-limit, paging-direction,
  bar-open, and timeframe table. An unsupported timeframe raises
  `NotSupportedError`.

## [0.3.0] - 2026-09-10

Hardening pass on the OKX adapter, driven by six **real-money OKX runs** on
2026-09-10 (`ledger.jsonl`, 321 records, 32 rejected orders). Every item below
answers something that actually went wrong on a live account. Recorded
request/response pairs from those runs are kept in
`tests/fixtures/okx/live_20260910.json` and replayed as contract tests
(`tests/exchanges/test_okx_live_replay.py`) — no live keys, no network.

### Fixed

- **`*_sync` twins now survive repeated calls on the same instance.** Each
  twin runs its call in a fresh `asyncio.run` loop, but the `httpx.AsyncClient`
  (and its connection pool) was built once in `HTTPClient.__init__` and bound
  to the first loop — so the **second** call died with `RuntimeError: Event
  loop is closed`. `HTTPClient` now tracks the loop its client belongs to and
  rebuilds the client **and its transport** when the loop changes or the client
  was closed; each sync twin releases the pool before its loop dies. The rate
  limiter is deliberately **not** rebuilt — its token budget belongs to the
  `HTTPClient`, not to one loop (see below).
- **An injected transport survives the rebuild — it used to be silently
  dropped.** Recorded-fixture tests installed a mock by assigning a finished
  `httpx.AsyncClient` to `HTTPClient._client`. Because the rebuild in `_bind()`
  only knows about `transport_factory`, the **second** `*_sync` call built a
  real client and went out to the live venue: a probe on 2026-09-10 saw call 1
  return the mocked `last=1.0` and call 2 return `78048.9` — a genuine OKX
  price — from an `AsyncHTTPTransport`. Injecting a client object is now
  rejected (`_client` is read-only); `HTTPClient.set_transport_factory()` is the
  one supported path, and a factory that returns `None` raises rather than
  letting httpx substitute a real transport.
- **The rate limiter is no longer reset on every `*_sync` call.** `_bind()`
  rebuilt the `RateLimiter` along with the client, and a fresh bucket starts
  full — so every sync call was a "first" call and the limit did not exist.
  Measured on 2026-09-10: 40 calls took 0.03 s via the sync twins against
  3.02 s via `await` at `rate=10/s`; both are 3.0 s now. Only the loop-bound
  `asyncio.Lock` is rebuilt when the running loop changes; the token budget
  carries over.
- **Spot `tdMode` is decided from the measured account level, never assumed.**
  It was hard-coded to `"cash"`, which an `acctLv=3` (multi-currency margin)
  account rejects outright — `51000 Parameter tdMode error` wiped out four
  live scenarios. `create_order` now measures `acctLv` once via
  `fetch_account_config()` (1/2 -> `cash`, 3/4 -> `cross`) and **raises rather
  than sending an order** when the level is unknown or the lookup fails.
- **Order rejections keep their reason.** A rejected OKX order arrives as HTTP
  200 with top-level `code: "1"`, an empty `msg`, and the real reason in
  `data[0].sCode`/`sMsg`. Reading the top-level code first reported every
  rejection as the meaningless code `"1"`; order/cancel responses now read
  `sCode` first.
- `Position` parsing: hedge-mode rows (`posSide`) are read correctly, and a
  zero position is `"flat"` instead of being reported as a short.

### Added

- `create_order(..., client_order_id=...)` — OKX `clOrdId`, the venue-level
  idempotency key, validated (1-32 alphanumeric) before the request goes out.
  pycex never generates one: the key is the caller's policy. `Order` gains
  `client_order_id`.
- `create_order(..., reduce_only=, tgt_ccy=, tp_px=, sl_px=)` — reduce-only
  (SWAP), attached take-profit/stop-loss (`attachAlgoOrds`), and the spot
  market-order size unit. A spot market order **always** states `tgtCcy`
  (buy -> `quote_ccy`, sell -> `base_ccy`) instead of relying on the venue
  default.
- `set_leverage(symbol, lever, mgn_mode)` (`BaseExchange` contract + OKX
  implementation, with a `set_leverage_sync` twin). No leverage cap lives in
  the SDK — that is the caller's risk policy.
- `fetch_available_balance(asset)` (`BaseExchange` contract + OKX per-currency
  `availBal` implementation, with a sync twin). Re-measures on every call and
  never caches: this is the only honest answer to "has my last fill settled".
- `Position.margin_mode` (`cross`/`isolated` as reported, `None` when the venue
  did not say) and `Position.signed_amount`. `Position.amount` is documented as
  **absolute** — direction lives in `side` alone.
- `fetch_account_config()` on OKX — `acctLv`, `posMode`, `perm`, `kycLv` for a
  read-only connectivity probe; `OKX.account_level` exposes the measured level
  read-only.
- `PyCexError.retryable` and `SettlementPendingError` — OKX `51008` is
  classified as "not settled **yet**" (retryable, still an
  `InsufficientBalanceError`) rather than a flat "insufficient funds". pycex
  classifies only; it never retries and holds no wait policy.

## [0.2.0] - 2026-08-30

### Added

- **Upbit**, **Bithumb**, **Korbit** — three new Korean-won (KRW) spot
  exchange adapters, full unified interface (tickers, order book, candles,
  trades, markets, balance, place/cancel/fetch orders, my trades; sync +
  async). None has a sandbox/demo environment; `sandbox=True` or
  `market_type="linear"` raises `NotSupportedError` on all three.
- **USDT-margined linear perpetual support** for **Binance**, **OKX**, and
  **Bitget** via `market_type="linear"` — linear symbols use canonical
  `BASE/QUOTE:SETTLE` notation (e.g. `BTC/USDT:USDT`). New unified methods on
  these three: `fetch_positions`, `fetch_funding_rate`. **Bybit** also gains
  `market_type="linear"`/`category="linear"` coverage for markets, candles,
  orders, balance, and my_trades — `fetch_positions`/`fetch_funding_rate`
  are not wired for Bybit (out of phase-1 scope for that adapter; still
  raise `NotSupportedError`). `fetch_my_trades` is now part of the base
  interface for every adapter.
- `fetch_markets()` — unified `Market` model (native symbol, base/quote,
  price tick, amount step, min notional, active flag) on every adapter.
- Candle pagination via `since=`/`until=` on `fetch_candles` — pages over each
  adapter's `_fetch_candles_page` hook, dedupes, sorts ascending and cuts at
  `until`. The page walk follows `BaseExchange.candle_paging`, set per venue
  from a live probe rather than from the docs: **forward** (cursor is `since`,
  oldest slice first) on Binance spot/linear; **backward** (cursor is `until`
  or the venue's "now", newest slice first) on Upbit/Bithumb (`to`), Korbit
  (`end`), OKX (`after`), Bitget spot+mix (`endTime`) and Bybit (`end`).
  Page limit is 200 bars everywhere except OKX (100). Verified live on
  2026-08-30 across all 11 exchange/market-type surfaces: 1h bars over 15 days
  = 360/360, 1d bars over 300 days = 300/300, ascending, no duplicates
  (`pytest -m live tests/live -k pagination`).
- Canonical symbol notation everywhere: spot `BASE/QUOTE`, linear perpetual
  `BASE/QUOTE:SETTLE` (`pycex.symbols.parse_symbol`/`Symbol`).
- Unified `sandbox: bool` flag on every adapter constructor, replacing the
  per-exchange `testnet=`/`demo=` kwargs (see Deprecated).
- `pycex.factory.create_exchange(name, *, api_key, secret, passphrase,
  sandbox, market_type)` — single construction path shared by the CLI and
  MCP server, with `PYCEX_{EXCHANGE}_API_KEY`/`_SECRET`/`_PASSPHRASE`
  per-exchange env var fallback.
- CLI: `--sandbox`/`--market-type`, and support for all seven exchanges
  through the shared factory.
- MCP server: all seven exchanges (`get_ticker`/`get_order_book`/
  `get_balance`/`place_order`/`cancel_order` now go through
  `pycex.factory.create_exchange`), plus multi-exchange `compare_prices` and
  `aggregate_balance` tools.
- New unified Pydantic models: `Market`, `MyTrade`, `Position`, `FundingRate`.
- Live smoke test suite (`@pytest.mark.live`, `tests/live/`) exercising
  public/authenticated endpoints on real exchange APIs across all 11
  exchange/market-type surfaces, plus a `nightly-live` GitHub Actions
  workflow (`workflow_dispatch` + daily cron, no secrets required for the
  public-surface subset).
- **Bitget** spot exchange (V2 API) — `Bitget(api_key, secret, passphrase, *, demo=False)`.
  Implements the full unified interface (tickers, order book, candles, trades,
  balance, place/cancel/fetch orders; sync + async). `demo=True` routes private
  calls to Bitget demo (simulated) trading via the `paptrading: 1` header.

### Changed

- **Sync methods are now auto-generated.** Every public async method
  (`fetch_ticker`, `fetch_order_book`, `fetch_candles`, `fetch_trades`,
  `fetch_markets`, `fetch_balance`, `create_order`, `cancel_order`,
  `fetch_order`, `fetch_open_orders`, `fetch_my_trades`, `fetch_positions`,
  `fetch_funding_rate`) gets a blocking `<name>_sync` twin generated by
  `BaseExchange.__init_subclass__` — hand-written sync wrappers were removed
  from every adapter.
- `HTTPClient.get`/`.post` now return `Any` (adapters parse the exchange's
  own response shape); added `HTTPClient.post_raw` (sends an exact
  caller-supplied body string, byte-for-byte, for exchanges whose signature
  covers the literal request body), `post_form`
  (`application/x-www-form-urlencoded`, for Korbit's signed-body contract),
  and `delete`. `HTTPClient(error_mapper=...)` lets each adapter translate
  its own error-body shape into the shared exception hierarchy.
- **Breaking:** canonical symbols (`BASE/QUOTE` / `BASE/QUOTE:SETTLE`) are now
  required on every public per-symbol method, on all seven adapters — a
  caller passing an exchange-native symbol (e.g. `"BTCUSDT"`, `"BTC-USDT"`,
  `"KRW-BTC"`) gets `SymbolNotFoundError` instead of a successful call. Every
  returned model's `.symbol` field is likewise always canonical.

### Deprecated

- `testnet=`/`demo=` constructor kwargs on every adapter — use `sandbox=`.
  Passing either still works but emits a `DeprecationWarning` and maps onto
  `sandbox=`.
- CLI `--testnet` flag — use `--sandbox`. Emits a `DeprecationWarning`.

### Fixed

- **Binance**: `cancel_order` bypassed the shared error-mapping path on a
  non-2xx response.
- **OKX, Bitget, Bybit**: signed-body/query mismatch that broke **every real
  POST order** — httpx's own `json=` encoding re-serializes a dict with
  different whitespace than `json.dumps`, which silently invalidated the
  HMAC signature on every signed `create_order`/`cancel_order` call on all
  three exchanges. Fixed via `HTTPClient.post_raw` (OKX, Bitget — signs and
  sends the identical JSON string) and by signing the exact GET query string
  as sent, not a re-sorted copy (Bybit).
- **Bybit**: a `"list"`-keyed response shape regression in the shared
  response-unwrapping path.
- **OKX, Bitget (spot)**: daily (`1d`) candles requested the bare
  `bar=1D`/`granularity=1day` granularity, which both exchanges align to
  Hong Kong time (UTC+8), not UTC — every daily bar landed 8 hours off the
  `Candle.timestamp` = UTC-midnight contract. Fixed by requesting the
  `...utc`-suffixed granularity (`1Dutc`) on both.
- **All seven adapters**: `fetch_candles(since=..., until=...)` silently
  truncated any range longer than one page on every venue except Binance —
  the page walk assumed a forward `since` cursor, which five of the seven
  ignore. Requesting 500 daily bars returned 200 (Upbit, Korbit), 100 (OKX)
  or 199 (Bybit). See the `candle_paging` entry under Added.
- **Bithumb**: `fetch_candles(since=..., until=...)` raised `TypeError` — the
  `to` cursor was sent with a `Z` suffix, which Bithumb rejects (HTTP 200 +
  `{"error":{"name":400,...}}`); it reads `to` as naive **KST** where Upbit
  reads it as **UTC**. The formatter is now a per-exchange hook.
- **Upbit, Bithumb**: neither could raise a `PyCexError` for a bad request.
  Bithumb serves error envelopes with HTTP **200**, which never reached the
  error mapper (`fetch_ticker("NOPE/KRW")` died with `KeyError`), and both
  return an **integer** `error.name` for an unknown market, which the mapper's
  substring test blew up on (`TypeError: argument of type 'int' is not
  iterable`). Envelopes are now checked on every response of any status, and
  an unknown market raises `SymbolNotFoundError`.
- **Binance**: public `fetch_trades` reported the maker side —
  `isBuyerMaker=true` means the taker **sold**. Every other adapter reports
  the taker side.
- **Bybit**: `fetch_candles(symbol, tf, limit=n)` returned bars newest-first
  while every other adapter returned them ascending.
- **Bitget**: the spot candle page was returned unsorted (the mix page was
  sorted), so bar ordering depended on `market_type`.
- **OKX**: an empty `data` array raised `IndexError` on `fetch_ticker` and
  returned an empty order book on `fetch_order_book`; both raise
  `ExchangeError` now.
- `close_sync()` / `with Exchange() as ex:` closed an unused sync HTTP client
  and left the async client — the one every `*_sync` wrapper actually drives —
  open. The dead sync client and its `sync_get`/`sync_post`/`sync_delete`
  methods are gone.
- CLI/MCP help advertised exchange-native symbol examples (`BTCUSDT`), which
  v0.2 rejects; `Balance.raw` on bare-list balance endpoints carried a
  fabricated `{"balances": ...}` envelope; only 4 of the 12 models were
  re-exported from the package root.
- `mcp` dependency pinned to `mcp[cli]>=1.0.0,<2` to avoid an untested major
  version.

## [0.1.0] - 2025-05-19

### Added

- Binance exchange adapter with full spot API support
- Bybit V5 exchange adapter with full spot API support
- OKX V5 exchange adapter with full spot API support
- Unified Pydantic models: `Ticker`, `OrderBook`, `Balance`, `Order`, `Candle`, `Trade`
- Sync and async API for all exchange operations
- Context manager support (`with` / `async with`) for automatic cleanup
- Token bucket rate limiter built into `HTTPClient`
- MCP server for Claude Desktop and AI assistant integration
- CLI tool (`pycex`) for terminal-based exchange operations
- PEP 561 `py.typed` marker for full type safety
- Exception hierarchy: `PyCexError`, `ExchangeError`, `RateLimitError`, etc.
- Testnet/demo mode support for all exchanges
- `.raw` field on all models for accessing original exchange responses

[Unreleased]: https://github.com/Jaeminyx-Stoa/pycex/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Jaeminyx-Stoa/pycex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Jaeminyx-Stoa/pycex/releases/tag/v0.1.0

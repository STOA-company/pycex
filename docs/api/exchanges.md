# Exchanges

All exchange classes implement the `BaseExchange` interface. You can swap
exchanges without changing your application logic. Symbols are always the
canonical `BASE/QUOTE` notation (e.g. `BTC/USDT`) — each adapter converts it
to that exchange's own native notation internally before calling the API.

## Binance

```python
from pycex import Binance

# Public data (no auth)
with Binance() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    ob = ex.fetch_order_book_sync("BTC/USDT", limit=10)
    print(f"BTC: ${ticker.last:,.2f}")

# With authentication
with Binance(api_key="KEY", secret="SECRET") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    canceled = ex.cancel_order_sync(order.id, "BTC/USDT")
```

### Binance Constructor

```python
Binance(
    api_key: str = "",
    secret: str = "",
    *,
    sandbox: bool = False,
    market_type: MarketType = "spot",
    testnet: bool | None = None,  # deprecated, use sandbox=
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Binance API key |
| `secret` | `str` | `""` | Binance API secret |
| `sandbox` | `bool` | `False` | Use testnet (`testnet.binance.vision`) |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Product type |
| `testnet` | `bool \| None` | `None` | Deprecated alias for `sandbox` |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### Binance Testnet

```python
from pycex import Binance

with Binance(api_key="TESTNET_KEY", secret="TESTNET_SECRET", sandbox=True) as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    print(f"Testnet BTC: ${ticker.last:,.2f}")

    balance = ex.fetch_balance_sync()
    btc = balance.get("BTC")
    if btc:
        print(f"BTC balance: {btc.free}")
```

### Binance USDT-M Perpetuals (`market_type="linear"`)

`market_type="linear"` switches the base URL to `fapi.binance.com` (sandbox:
`testnet.binancefuture.com`) and every endpoint to its `/fapi/v1`/`/fapi/v2`
counterpart. Linear symbols use `BASE/QUOTE:QUOTE` notation, e.g. `BTC/USDT:USDT`.

```python
from pycex import Binance

with Binance(api_key="KEY", secret="SECRET", market_type="linear") as ex:
    markets = ex.fetch_markets_sync()  # populates the symbol cache used by from_native
    funding = ex.fetch_funding_rate_sync("BTC/USDT:USDT")
    print(f"funding rate: {funding.rate:.6f} (next {funding.next_funding_time})")

    positions = ex.fetch_positions_sync()
    for p in positions:
        print(f"{p.symbol}: {p.side} {p.amount} @ {p.entry_price}, uPnL={p.unrealized_pnl}")

    order = ex.create_order_sync("BTC/USDT:USDT", "buy", "market", 0.001)
    trades = ex.fetch_my_trades_sync("BTC/USDT:USDT")
```

- `fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on a `"spot"`
  instance (the shared `BaseExchange` default) — they only work with
  `market_type="linear"`.
- `fetch_my_trades` **requires** `symbol` on Binance (both spot and linear) and
  raises `ValueError` if omitted — Binance's `myTrades`/`userTrades` endpoints
  have no all-symbols mode.
- `create_order` assumes one-way mode and maps Binance code `-4061` to
  `HedgeModeNotSupportedError`. See the [futures order safety guide](../futures-order-safety.md)
  for reduce-only, client ID lookup, normalized order results and sizing limits.
- Linear account balance (`GET /fapi/v2/balance`) has no `locked` field; it is
  derived as `balance - availableBalance` (funds tied up in position
  margin/unrealized loss).

## Bybit

Bybit uses the V5 API. Its native symbol format is the same as Binance
(`BTCUSDT`), but callers always pass the canonical `BTC/USDT` form.

```python
from pycex import Bybit

# Public data
with Bybit() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    ob = ex.fetch_order_book_sync("BTC/USDT", limit=10)
    print(f"BTC: ${ticker.last:,.2f}")

# With authentication
with Bybit(api_key="KEY", secret="SECRET") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", 0.001, 50000.0)
```

### Bybit Constructor

```python
Bybit(
    api_key: str = "",
    secret: str = "",
    *,
    sandbox: bool = False,
    market_type: MarketType = "spot",
    testnet: bool | None = None,  # deprecated, use sandbox=
    timeout: float = 30.0,
    category: str | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Bybit API key |
| `secret` | `str` | `""` | Bybit API secret |
| `sandbox` | `bool` | `False` | Use testnet (`api-testnet.bybit.com`) |
| `market_type` | `"spot" \| "linear"` | `"spot"` | `"linear"` maps `category` to `"linear"` unless `category=` is given explicitly |
| `testnet` | `bool \| None` | `None` | Deprecated alias for `sandbox` |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |
| `category` | `str \| None` | `None` | Product category (`spot`, `linear`, `inverse`); overrides `market_type` when set |

`market_type="linear"` (or `category="linear"`) covers `fetch_markets`,
`fetch_candles`, `create_order`/`cancel_order`/`fetch_order`/
`fetch_open_orders`, `fetch_balance`, and `fetch_my_trades`.
**`fetch_positions`/`fetch_funding_rate` are intentionally left at the
`BaseExchange` default (raise `NotSupportedError`) — out of phase-1 scope
for this adapter**, unlike Binance/OKX/Bitget's linear support.

### Bybit Async Example

```python
import asyncio
from pycex import Bybit

async def main():
    async with Bybit() as ex:
        ticker = await ex.fetch_ticker("BTC/USDT")
        candles = await ex.fetch_candles("BTC/USDT", "1h", limit=10)
        trades = await ex.fetch_trades("BTC/USDT", limit=5)

        print(f"BTC: ${ticker.last:,.2f}")
        for c in candles[:3]:
            print(f"  O={c.open:,.2f} H={c.high:,.2f} L={c.low:,.2f} C={c.close:,.2f}")
        for t in trades[:3]:
            print(f"  {t.side} {t.amount} @ ${t.price:,.2f}")

asyncio.run(main())
```

## OKX

OKX uses the V5 API. Its native symbol format uses dashes (`BTC-USDT`), but
callers always pass the canonical `BTC/USDT` form.

```python
from pycex import OKX

# Public data
with OKX() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    ob = ex.fetch_order_book_sync("BTC/USDT", limit=10)
    print(f"BTC: ${ticker.last:,.2f}")

# With authentication (OKX requires passphrase)
with OKX(api_key="KEY", secret="SECRET", passphrase="PASS") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", 0.001, 50000.0)
```

### OKX Constructor

```python
OKX(
    api_key: str = "",
    secret: str = "",
    passphrase: str = "",
    *,
    sandbox: bool = False,
    market_type: MarketType = "spot",
    td_mode: Literal["cross", "isolated"] = "cross",
    demo: bool | None = None,  # deprecated, use sandbox=
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | OKX API key |
| `secret` | `str` | `""` | OKX API secret |
| `passphrase` | `str` | `""` | OKX API passphrase |
| `sandbox` | `bool` | `False` | Use demo trading mode |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Product type (`"linear"` = USDT-margined perpetual SWAP) |
| `td_mode` | `"cross" \| "isolated"` | `"cross"` | Margin mode sent as `tdMode` on SWAP orders (ignored on spot, which always uses `"cash"`) |
| `demo` | `bool \| None` | `None` | Deprecated alias for `sandbox` |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### OKX Demo Trading

```python
from pycex import OKX

# Demo mode uses a header flag, same API endpoint
with OKX(api_key="KEY", secret="SECRET", passphrase="PASS", sandbox=True) as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    print(f"Demo BTC: ${ticker.last:,.2f}")

    balance = ex.fetch_balance_sync()
    for asset in balance.assets:
        print(f"  {asset.asset}: {asset.free}")
```

### OKX USDT-Margined Perpetual SWAP (`market_type="linear"`)

Both market types hit the same host (`www.okx.com`); only `instType`
(`SPOT`/`SWAP`) and a few SWAP-only endpoints differ. Linear symbols use
`BASE/QUOTE:QUOTE` notation, e.g. `BTC/USDT:USDT` <-> native `BTC-USDT-SWAP`.

```python
from pycex import OKX

with OKX(api_key="KEY", secret="SECRET", passphrase="PASS", market_type="linear") as ex:
    markets = ex.fetch_markets_sync()  # populates the symbol cache used by from_native
    funding = ex.fetch_funding_rate_sync("BTC/USDT:USDT")
    print(f"funding rate: {funding.rate:.6f} (next {funding.next_funding_time})")

    positions = ex.fetch_positions_sync()
    for p in positions:
        print(f"{p.symbol}: {p.side} {p.amount} @ {p.entry_price}, uPnL={p.unrealized_pnl}")

    order = ex.create_order_sync("BTC/USDT:USDT", "buy", "market", 1)  # `1` = 1 contract, not 1 BTC
    trades = ex.fetch_my_trades_sync("BTC/USDT:USDT")
```

- `fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on a
  `"spot"` instance (the shared `BaseExchange` default) — they only work with
  `market_type="linear"`.
- **`sz` is in contracts, not base-asset quantity**, for SWAP instruments —
  `Market.contract_size` exposes `ctVal * ctMult` in base units.
  `Market.amount_from_base` converts base quantity to contract count and rejects
  below-minimum/off-step requests without rounding. `create_order` still accepts
  contract count directly; conversion is caller-controlled.
- `create_order` assumes one-way mode. The specific `51000 Parameter posSide error`
  maps to `HedgeModeNotSupportedError`; other parameter errors keep their type.
- **Inverse (coin-margined) perpetuals are out of scope** — only
  USDT-settled linear (`settle == quote`) is supported. `to_native` raises
  `SymbolNotFoundError` for a canonical symbol like `BTC/USD:BTC`, and
  `from_native` raises it for a native `*-USD-SWAP` symbol.
- `fetch_positions` maps OKX's `posSide`: `"net"` mode uses the sign of
  `pos` to decide long/short; explicit hedge-mode `"long"`/`"short"` rows are
  used as-is. Flat rows (`pos == 0`) are excluded. `liqPx`/`avgPx`/`lever`
  of `"0"`/empty map to `None`, not `0.0`.
- `fetch_my_trades` calls `GET /api/v5/trade/fills` (last 3 days only). That
  endpoint's `after`/`before` params page over a `billId`, not a timestamp,
  so `since=` is applied client-side after parsing rather than as a
  server-side range filter.
- `fetch_candles`/`_fetch_candles_page` automatically retries against
  `GET /api/v5/market/history-candles` with the same params whenever the
  regular `/api/v5/market/candles` endpoint returns an empty `data` array —
  that endpoint only serves a recent rolling window, and empty is how it
  signals "ask history-candles instead" rather than returning an error.
  **History backfill pages are capped at 100 bars** (`history-candles`'
  `limit` maximum) — `OKX.candle_page_limit = 100`, and `_fetch_candles_page`
  clamps any larger `limit` down to 100 before either call, even though the
  recent `/market/candles` endpoint itself would accept up to 300.
- `create_order`/`cancel_order` sign and send the **identical** JSON string
  (`HTTPClient.post_raw`, not `post`) — OKX's signature covers the literal
  request body bytes, and httpx's own `json=` encoding re-serializes a dict
  with different separators than `json.dumps`, which would silently break
  every signed order/cancel. A rejection on either call can also arrive as
  HTTP 200 + top-level `code == "0"` with the real failure in
  `data[0].sCode`/`sMsg` (e.g. `sCode: "51008"` for insufficient balance) —
  both methods check `sCode` after the top-level check and raise through the
  same error mapping.

## Bitget

Bitget uses the V2 API. Spot and USDT-margined linear perpetual futures
("mix", `market_type="linear"`) share the same host (`api.bitget.com`) but
different path prefixes (`/api/v2/spot/...` vs `/api/v2/mix/...`); linear
requests additionally require a `productType` param (`"USDT-FUTURES"`, or
`"SUSDT-FUTURES"` in sandbox mode). Native symbols have no separator
(`BTCUSDT`), for both market types; callers always pass the canonical
`BTC/USDT` (spot) or `BTC/USDT:USDT` (linear) form.

```python
from pycex import Bitget

# Public data
with Bitget() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    ob = ex.fetch_order_book_sync("BTC/USDT", limit=10)
    print(f"BTC: ${ticker.last:,.2f}")

# With authentication (Bitget requires passphrase)
with Bitget(api_key="KEY", secret="SECRET", passphrase="PASS") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", 0.001, 50000.0)
```

### Bitget Constructor

```python
Bitget(
    api_key: str = "",
    secret: str = "",
    passphrase: str = "",
    *,
    sandbox: bool = False,
    market_type: MarketType = "spot",
    demo: bool | None = None,  # deprecated, use sandbox=
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Bitget API key |
| `secret` | `str` | `""` | Bitget API secret |
| `passphrase` | `str` | `""` | Bitget API passphrase |
| `sandbox` | `bool` | `False` | Use demo trading mode |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Product type (`"linear"` = USDT-margined perpetual futures, `productType="USDT-FUTURES"`) |
| `demo` | `bool \| None` | `None` | Deprecated alias for `sandbox` |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### Bitget Demo Trading — two independent mechanisms

Unlike most exchanges, Bitget's sandbox/demo mode differs by `market_type`
because Bitget itself exposes two unrelated demo mechanisms and this adapter
picks a different one per market type (confirmed via ccxt's own request
builder — Bitget's `api-doc` site is a client-rendered SPA that would not
serve endpoint reference pages to automated fetching in this pass):

- **Spot** (`sandbox=True`, `market_type="spot"`): header-based demo trading
  — every signed request gets an extra `paptrading: 1` header, same host,
  same symbols, requires a demo API key issued in Bitget's demo environment.
- **Linear** (`sandbox=True`, `market_type="linear"`): a dedicated simulated
  productType family instead — `productType` becomes `"SUSDT-FUTURES"` and
  native symbols get an `S` prefix on **both** base and quote
  (`BTC/USDT:USDT` -> `SBTCSUSDT`, not `BTCUSDT`). The `paptrading` header is
  **not** also added in this case — the `S*` productType is itself Bitget's
  demo signal, and combining both would be redundant/conflicting.

```python
from pycex import Bitget

# Spot demo: header flag, same symbols
with Bitget(api_key="KEY", secret="SECRET", passphrase="PASS", sandbox=True) as ex:
    balance = ex.fetch_balance_sync()

# Linear demo: SUSDT-FUTURES productType, S-prefixed native symbols
with Bitget(api_key="KEY", secret="SECRET", passphrase="PASS", market_type="linear", sandbox=True) as ex:
    print(ex.to_native("BTC/USDT:USDT"))  # "SBTCSUSDT"
```

### Bitget USDT-Margined Linear Perpetual Futures (`market_type="linear"`)

```python
from pycex import Bitget

with Bitget(api_key="KEY", secret="SECRET", passphrase="PASS", market_type="linear") as ex:
    markets = ex.fetch_markets_sync()  # populates the symbol cache used by from_native
    funding = ex.fetch_funding_rate_sync("BTC/USDT:USDT")
    print(f"funding rate: {funding.rate:.6f} (every {funding.interval_hours}h)")

    positions = ex.fetch_positions_sync()
    for p in positions:
        print(f"{p.symbol}: {p.side} {p.amount} @ {p.entry_price}, uPnL={p.unrealized_pnl}")

    order = ex.create_order_sync("BTC/USDT:USDT", "buy", "market", 0.001)
    trades = ex.fetch_my_trades_sync("BTC/USDT:USDT")
```

- `fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on a
  `"spot"` instance (the shared `BaseExchange` default) — they only work with
  `market_type="linear"`.
- `create_order` keeps `marginMode="crossed"` and checks `posMode` before every
  linear order. Hedge mode raises `HedgeModeNotSupportedError` before submission;
  an unknown mode also prevents submission. The check adds one signed account GET.
- **Inverse (coin-margined) perpetuals are out of scope** — only
  USDT-settled linear (`settle == quote`) is supported. `to_native` raises
  `SymbolNotFoundError` for a canonical symbol like `BTC/USD:BTC`.
- `fetch_positions` filters out flat rows (`total == 0`); `liquidationPrice`
  of `"0"`/empty maps to `None`, not `0.0`, same treatment as OKX/Binance.
- `fetch_my_trades`/`fetch_open_orders` unwrap a nested key
  (`data.fillList`/`data.entrustedList`) rather than a bare list under
  `data` — confirmed against ccxt's parser, since Bitget's own `api-doc`
  pages could not be reached by automated fetching in this pass. Both
  require a `symbol` (raises `ValueError` without one).
- `fetch_candles`/`_fetch_candles_page` clamp `limit` to 200 (the tighter of
  the "recent" `market/candles` endpoint's 1000-bar cap and
  `history-candles`' 200-bar cap — `Bitget.candle_page_limit = 200`) and
  retry against `GET /api/v2/mix/market/history-candles` with the same
  params whenever `market/candles` returns an empty `data` array (mirrors
  the OKX adapter's fallback; not independently confirmed live for Bitget in
  this pass, but harmless when the recent endpoint already has data). Daily
  bars use granularity `"1Dutc"` (UTC-aligned), not `"1D"`/`"1day"`.
- `create_order`/`cancel_order` sign and send the **identical** JSON string
  (`HTTPClient.post_raw`, not `post`) for both spot and linear — same
  signature-body trap as OKX: Bitget's `ACCESS-SIGN` covers the literal
  request body bytes, and httpx's own `json=` encoding re-serializes a dict
  with different separators than `json.dumps`, which would silently break
  every signed order/cancel/balance/position/fills request. Unlike OKX,
  Bitget's place-order/cancel-order responses have no secondary per-item
  rejection field — the top-level `code` (already `"00000"`-checked) is the
  only rejection signal for these two endpoints.

## Upbit

Upbit is a Korean-won (KRW) spot exchange — no sandbox/demo environment and
spot only (`market_type` must be `"spot"`, `sandbox=True` raises
`NotSupportedError`). Its native symbol format is `QUOTE-BASE` (e.g. `KRW-BTC`
for `BTC/KRW`), the reverse of most exchanges. Private endpoints are signed
with a JWT (HS256) rather than an HMAC header.

```python
from pycex import Upbit

# Public data (no auth)
with Upbit() as ex:
    ticker = ex.fetch_ticker_sync("BTC/KRW")
    ob = ex.fetch_order_book_sync("BTC/KRW", limit=10)
    print(f"BTC: ₩{ticker.last:,.0f}")

# With authentication
with Upbit(api_key="KEY", secret="SECRET") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/KRW", "buy", "limit", 0.001, 100_000_000.0)
    canceled = ex.cancel_order_sync(order.id, "BTC/KRW")
```

### Upbit Constructor

```python
Upbit(
    api_key: str = "",
    secret: str = "",
    *,
    sandbox: bool = False,      # always False — raises NotSupportedError if True
    market_type: MarketType = "spot",  # must be "spot" — raises NotSupportedError otherwise
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Upbit access key |
| `secret` | `str` | `""` | Upbit secret key |
| `sandbox` | `bool` | `False` | Not supported — Upbit has no demo environment |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Only `"spot"` is supported |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### Upbit Notes

- **Market orders**: a market *buy* uses Upbit's `ord_type="price"` — the
  `amount` argument to `create_order` is then the **KRW total to spend**, not
  a BTC quantity. A market *sell* uses `ord_type="market"` with `amount` as
  the base-asset volume, matching every other adapter.
- **`fetch_my_trades`**: Upbit has no dedicated fills endpoint. It flattens
  the `trades` array of completed (`state=done`) orders instead; `fee` is
  always `0.0` (not prorated from the order's `paid_fee`) — see `raw` for the
  original trade payload.
- **Daily candles reset at 00:00 UTC (09:00 KST)**, not KST midnight —
  different from Bithumb/Korbit, which reset at KST midnight.

## Bithumb

Bithumb is a Korean-won (KRW) spot exchange — no sandbox/demo environment and
spot only, same as Upbit. Its public market-data surface (symbols, candles,
ticker, order book, public trades) is identical in path and shape to Upbit's,
so both adapters share that code (`KrwV1Mixin`). Private endpoints, however,
are a **v1/v2 mix**: balance uses the same `/v1/accounts` as Upbit, but order
creation/cancel/pending/history use newer `/v2/...` paths with a leaner
response shape, while individual-order lookup stays on the legacy `/v1/order`.
Auth is a JWT (HS256) like Upbit's, but the payload requires an explicit
`timestamp` (ms) field that Upbit's omits.

```python
from pycex import Bithumb

# Public data (no auth)
with Bithumb() as ex:
    ticker = ex.fetch_ticker_sync("BTC/KRW")
    ob = ex.fetch_order_book_sync("BTC/KRW", limit=10)
    print(f"BTC: ₩{ticker.last:,.0f}")

# With authentication
with Bithumb(api_key="KEY", secret="SECRET") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/KRW", "buy", "limit", 0.001, 100_000_000.0)
    canceled = ex.cancel_order_sync(order.id, "BTC/KRW")
```

### Bithumb Constructor

```python
Bithumb(
    api_key: str = "",
    secret: str = "",
    *,
    sandbox: bool = False,      # always False — raises NotSupportedError if True
    market_type: MarketType = "spot",  # must be "spot" — raises NotSupportedError otherwise
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Bithumb access key |
| `secret` | `str` | `""` | Bithumb secret key |
| `sandbox` | `bool` | `False` | Not supported — Bithumb has no demo environment |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Only `"spot"` is supported |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### Bithumb Notes

- **Private endpoints mix v1 and v2 paths**: balance (`GET /v1/accounts`) and
  individual-order lookup (`GET /v1/order?uuid=`) are v1 and return the full
  order/account shape; order creation (`POST /v2/orders`), cancel
  (`DELETE /v2/order`), open orders (`GET /v2/orders/pending`), and completed
  orders (`GET /v2/orders/history`) are v2. The v2 field names (`order_id`,
  `order_type`) are normalized to their v1 equivalents (`uuid`, `ord_type`)
  before parsing, so all endpoints share one `Order` parser.
- **Market orders**: same convention as Upbit — a market *buy* uses
  `order_type="price"` with `amount` as the **KRW total to spend**; a market
  *sell* uses `order_type="market"` with `amount` as the base-asset volume.
- **`create_order`/`cancel_order` return partial data**: Bithumb's v2
  create/cancel responses don't echo back price/volume/state the way the v1
  full-order shape does, so the returned `Order`'s `amount`/`price`/`status`
  reflect only what the API actually returned (`cancel_order` forces
  `status="cancel"` since that's the one fact the call itself guarantees) —
  see `raw` for the full response.
- **`fetch_my_trades` makes `1 + N` requests**: `/v2/orders/history` has no
  `trades` array (only `trades_count`), so this fetches up to `limit`
  (default 20, capped at 50) completed orders, then calls
  `GET /v1/order?uuid=` once per order to flatten its `trades[]` array.
- 🚨 **Daily candles reset at 00:00 KST (15:00 UTC the previous day)** —
  different from Upbit, which resets at 00:00 UTC, despite both exchanges
  returning the same `candle_date_time_utc` field shape.

## Korbit

Korbit is a Korean-won (KRW) spot exchange (v2 REST) — no sandbox/demo
environment and spot only. Unlike Upbit/Bithumb (JWT-in-header), Korbit signs
with a single header (`X-KAPI-KEY`) plus two ordinary request *parameters*
that ride alongside every private call: `timestamp` (Unix ms) and `signature`
(HMAC-SHA256 hex over the exact encoded query string — GET/DELETE — or
`application/x-www-form-urlencoded` body — POST — that is actually sent,
`signature` itself excluded). Every response, public or private, is wrapped
`{"success": true/false, "data": ...}`.

🚨 **문서 확인일 2026-08-30, 서명 = HMAC-SHA256(secret, 전송될 쿼리스트링 또는 폼바디 문자열(signature 제외))**,
헤더는 `X-KAPI-KEY` 하나뿐이고 `timestamp`/`signature`는 헤더가 아니라 파라미터로 전송됨
(GET/DELETE=쿼리, POST=`application/x-www-form-urlencoded` 바디) —
`docs.korbit.co.kr/llms/en/rest_api.md` · `rest_api/trading.md` · `rest_api/quotation.md` 직접 열람으로 확인.

```python
from pycex import Korbit

# Public data (no auth)
with Korbit() as ex:
    ticker = ex.fetch_ticker_sync("BTC/KRW")
    candles = ex.fetch_candles_sync("BTC/KRW", "1d", limit=3)
    print(f"BTC: ₩{ticker.last:,.0f}")

# With authentication
with Korbit(api_key="KEY", secret="SECRET") as ex:
    balance = ex.fetch_balance_sync()
    order = ex.create_order_sync("BTC/KRW", "buy", "limit", 0.001, 100_000_000.0)
    canceled = ex.cancel_order_sync(order.id, "BTC/KRW")
```

### Korbit Constructor

```python
Korbit(
    api_key: str = "",
    secret: str = "",
    *,
    sandbox: bool = False,      # always False — raises NotSupportedError if True
    market_type: MarketType = "spot",  # must be "spot" — raises NotSupportedError otherwise
    timeout: float = 30.0,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_key` | `str` | `""` | Korbit API key (sent as `X-KAPI-KEY` header) |
| `secret` | `str` | `""` | Korbit API secret (HMAC-SHA256 signing key) |
| `sandbox` | `bool` | `False` | Not supported — Korbit has no demo environment |
| `market_type` | `"spot" \| "linear"` | `"spot"` | Only `"spot"` is supported |
| `timeout` | `float` | `30.0` | HTTP request timeout in seconds |

### Korbit Notes

- **Auth is param-based, not header-based**: only `X-KAPI-KEY` is a header;
  `timestamp` and `signature` are request parameters, computed by
  `pycex.auth.korbit_sign` over the *exact* string that will be sent
  (`HTTPClient.post_form` was added specifically so the signed bytes and the
  wire bytes are guaranteed identical — see its docstring).
- **Symbols**: `btc_krw` (lowercase, underscore) — different from
  Upbit/Bithumb's `KRW-BTC`.
- **Candle `interval`**: `1,5,15,60,240,1D` (minutes for the numeric values;
  `4h` → `240`, not `4h`). Confirmed against the live docs — the sidebar
  shorthand (`1m/5m/.../4h/1D`) the initial spec-sheet research flagged as
  unverified does **not** match the actual parameter values.
- 🚨 **`fetch_markets` active flag deviates from the original brief**: the real
  `GET /v2/currencyPairs` `status` field is `"launched"` / `"stopped"`, not
  `"active"` / `"inactive"` — confirmed against both the Task-0 fixture and a
  live docs fetch. `Market.active` is `True` iff `status == "launched"`.
- **Order amount parameter**: limit orders and market sells send `qty`
  (base-asset quantity); market buys send `amt` (quote-currency total to
  spend) — same buy/sell asymmetry as Upbit/Bithumb, different parameter names.
- **`fetch_open_orders`/`fetch_my_trades` require a symbol**: Korbit's API has
  no all-symbols listing for these (unlike Upbit/Bithumb); passing `symbol=None`
  raises `NotSupportedError`.
- **`cancel_order` returns unguessed side/type**: `DELETE /v2/orders` responds
  `{"success": true}` only, so the returned `Order`'s `side`/`type` stay `""`
  rather than being fabricated — see `raw` for the actual response.
- 🚨 **Daily candles reset at 00:00 KST (15:00 UTC the previous day)** — same
  boundary as Bithumb, different from Upbit (00:00 UTC). Confirmed by
  cross-checking identical epoch values against Bithumb's fixture (see
  `tests/fixtures/NOTES.md`).

## Daily-Bar Boundaries and Live-Smoke Notes (Task 13, 2026-08-30)

`fetch_candles(symbol, "1d", ...)`'s bar-open timestamp lands on a different
wall-clock boundary depending on the venue. All eleven exchange/market-type
surfaces below were exercised live (`pytest -m live tests/live`) against
real public endpoints from `mwork2`; `tests/live/test_smoke.py` asserts the
boundary in the `expected_offset` column programmatically (`c.timestamp %
86_400_000 == expected_offset`).

| Exchange | Market type(s) | Daily-bar boundary | `expected_offset` (ms) |
|---|---|---|---|
| Upbit | spot | 00:00 UTC (09:00 KST) | `0` |
| Bithumb | spot | 00:00 KST (15:00 UTC previous day) | `54_000_000` |
| Korbit | spot | 00:00 KST (15:00 UTC previous day) | `54_000_000` |
| Binance | spot, linear | 00:00 UTC | `0` |
| Bybit | spot, linear | 00:00 UTC | `0` |
| OKX | spot, linear | 00:00 UTC (**only** with `bar=1Dutc`; the bare `bar=1D` this adapter used before Task 13 aligns to Hong Kong time, UTC+8) | `0` |
| Bitget | spot, linear | 00:00 UTC (**only** with `granularity=1Dutc`; the bare `granularity=1day`/`1Dutc` for mix already matched, but spot used `1day` before Task 13, same UTC+8 bug as OKX) | `0` |

🚨 **Task 13 defect found live**: OKX (`_TIMEFRAME_MAP["1d"]`, both spot and
linear share one map) and Bitget spot (`_TIMEFRAME_MAP["1d"]`, mix already
used the correct `"1Dutc"`) both requested the bare `"1D"`/`"1day"`
granularity, which both exchanges align to **Hong Kong time (UTC+8)**, not
UTC — live-diffing `bar=1D` vs `bar=1Dutc` on the same OKX instrument showed
timestamps exactly `28_800_000`ms (8h) apart, and the same diff reproduced
on Bitget spot's `granularity=1day` vs `1Dutc`. Fixed by mapping `"1d"` to
the `"...utc"`-suffixed granularity in both adapters (`"1w"` is left as the
bare form — it is outside this library's timeframe contract
(`BaseExchange.supported_timeframes` is `1m/5m/15m/1h/4h/1d` only) and its
Hong-Kong-time behavior was not live-verified); see the
`_TIMEFRAME_MAP` docstring comments in each file and the mutation-verified
unit tests `test_fetch_candles_page_daily_uses_utc_suffixed_bar` (OKX) /
`test_fetch_candles_page_spot_daily_uses_utc_suffixed_granularity` (Bitget).

Other live-observed behavior, no code changes needed:

- All eleven surfaces' `fetch_order_book` returned a non-empty book (≥1 bid,
  ≥1 ask) for `BTC/KRW`/`BTC/USDT`(`:USDT`) — no venue returned an empty book
  for this liquid pair at test time.
- All eleven surfaces' `fetch_trades(limit=5)` returned exactly 5 recent
  trades; none of the eight global/linear surfaces or three KRW spot surfaces
  hit a rate limit during the smoke run (single request per surface, no
  retries observed).
- 🚨 The pagination claim this section used to make was worthless: a 10-day
  span of daily bars fits in a **single page** on every venue, so it passed
  while five of the seven adapters truncated every real multi-page range
  (2026-08-30, 500 daily bars requested: Upbit 200, Korbit 200, OKX 100,
  Bybit 199). The live case now spans more bars than one page on the venue it
  runs against — 1h bars over 15 days (360 bars, page limit 200 / OKX 100) on
  all ten global+KRW surfaces, and 1d bars over 300 days on Bithumb — and all
  eleven surfaces return the exact bar count, strictly ascending and
  duplicate-free. Per-venue paging direction (`BaseExchange.candle_paging`)
  and cursor parameters are tabulated in `README.md`.

## Unified Interface (BaseExchange)

All exchanges implement these methods:

### Market Data (no auth required)

```python
# Async
ticker = await ex.fetch_ticker("BTC/USDT")
ob = await ex.fetch_order_book("BTC/USDT", limit=20)
candles = await ex.fetch_candles("BTC/USDT", "1h", limit=100)
trades = await ex.fetch_trades("BTC/USDT", limit=100)

# Sync
ticker = ex.fetch_ticker_sync("BTC/USDT")
ob = ex.fetch_order_book_sync("BTC/USDT", limit=20)
```

### Account (auth required)

```python
# Async
balance = await ex.fetch_balance()

# Sync
balance = ex.fetch_balance_sync()
```

### Trading (auth required)

```python
# Async
order = await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
canceled = await ex.cancel_order(order.id, "BTC/USDT")
status = await ex.fetch_order(order.id, "BTC/USDT")
open_orders = await ex.fetch_open_orders("BTC/USDT")

# Sync
order = ex.create_order_sync("BTC/USDT", "buy", "limit", 0.001, 50000.0)
canceled = ex.cancel_order_sync(order.id, "BTC/USDT")
```

### Timeframes

Supported timeframes for candles (`BaseExchange.supported_timeframes`): `1m`,
`5m`, `15m`, `1h`, `4h`, `1d`. Requesting any other value raises
`NotSupportedError`.

```python
from pycex import Binance

with Binance() as ex:
    hourly = ex.fetch_ticker_sync("BTC/USDT")
    # Use with fetch_candles
    # candles = await ex.fetch_candles("BTC/USDT", "4h", limit=50)
```

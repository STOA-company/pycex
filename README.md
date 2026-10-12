# pycex

English | [한국어](README.ko.md)

Unified Python wrapper for cryptocurrency exchanges — **Binance**, **Bybit**,
**OKX**, **Bitget**, **Upbit**, **Bithumb**, **Korbit**.

A clean alternative to ccxt's sprawling codebase: Pydantic type safety, an async-first design, a built-in rate limiter, and an MCP server.

## Features

- **Unified API** — one interface for 7 exchanges (spot + USDT-margined perpetuals)
- **Sync + Async** — both styles supported (every method has an auto-generated `_sync` twin)
- **Canonical Symbols** — the same `BASE/QUOTE` (spot) / `BASE/QUOTE:SETTLE` (linear) notation on every exchange
- **Pydantic Models** — `Ticker`, `OrderBook`, `Balance`, `Order`, `Candle`, `Trade`, `Market`, `MyTrade`, `Position`, `FundingRate`
- **Rate Limiting** — built-in token-bucket rate limiter (per-exchange defaults)
- **MCP Server** — connect Claude Desktop and other AI assistants
- **CLI** — quotes, balances, and orders from the terminal
- **Type Safe** — PEP 561 `py.typed`, full type hints, passes `mypy --strict`

## Installation

```bash
pip install pycex

# with the MCP server
pip install pycex[mcp]
```

## Quick Start

Symbols always use canonical notation regardless of exchange — spot is
`BASE/QUOTE` (e.g. `BTC/USDT`), USDT perpetuals are `BASE/QUOTE:SETTLE` (e.g.
`BTC/USDT:USDT`). Each adapter converts to the exchange's native notation
(`BTCUSDT`, `BTC-USDT`, `KRW-BTC`, ...) internally, so passing a native symbol
directly raises `SymbolNotFoundError`.

```python
from pycex import Binance

# Ticker (no authentication needed)
with Binance() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    print(f"BTC: ${ticker.last:,.2f}")
    print(f"  Bid: ${ticker.bid:,.2f}  Ask: ${ticker.ask:,.2f}")
    print(f"  24h High: ${ticker.high:,.2f}  Low: ${ticker.low:,.2f}")
    print(f"  Volume: {ticker.volume:,.2f}")
```

Or via the shared factory, by exchange name (used internally by the CLI and MCP server):

```python
from pycex import create_exchange

with create_exchange("upbit") as ex:
    ticker = ex.fetch_ticker_sync("BTC/KRW")
    print(f"BTC: ₩{ticker.last:,.0f}")
```

## Multi-Exchange

Swap exchanges behind the same interface:

```python
from pycex import Binance, Bitget, Bybit, OKX

for ExchangeClass in [Binance, Bybit, OKX, Bitget]:
    with ExchangeClass() as ex:
        ticker = ex.fetch_ticker_sync("BTC/USDT")
        print(f"{ex.name}: BTC = ${ticker.last:,.2f}")
```

## Async Usage

```python
import asyncio
from pycex import Binance

async def main():
    async with Binance() as ex:
        ticker = await ex.fetch_ticker("BTC/USDT")
        ob = await ex.fetch_order_book("BTC/USDT", limit=5)
        candles = await ex.fetch_candles("BTC/USDT", "1h", limit=24)

        print(f"BTC: ${ticker.last:,.2f}")
        print(f"Best bid: ${ob.bids[0].price:,.2f} × {ob.bids[0].amount}")
        print(f"Last 24 candles: {len(candles)}")

asyncio.run(main())
```

## Order Book

```python
from pycex import Binance

with Binance() as ex:
    ob = ex.fetch_order_book_sync("ETH/USDT", limit=10)
    print("Asks:")
    for ask in ob.asks[:5]:
        print(f"  ${ask.price:,.2f} × {ask.amount:,.4f}")
    print("Bids:")
    for bid in ob.bids[:5]:
        print(f"  ${bid.price:,.2f} × {bid.amount:,.4f}")
```

## Trading

```python
from pycex import Binance

with Binance(api_key="YOUR_KEY", secret="YOUR_SECRET", sandbox=True) as ex:
    # Balance
    balance = ex.fetch_balance_sync()
    btc = balance.get("BTC")
    if btc:
        print(f"BTC: free={btc.free}, locked={btc.locked}, total={btc.total}")

    # Limit buy
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", amount=0.001, price=50000.0)
    print(f"Order placed: {order.id}")

    # Cancel
    canceled = ex.cancel_order_sync(order.id, "BTC/USDT")
    print(f"Canceled: {canceled.id}")
```

## USDT-Margined Perpetuals (`market_type="linear"`)

Binance, OKX, Bitget expose USDT-margined linear perpetual futures alongside
spot, selected via `market_type="linear"` on the constructor. Linear symbols
use `BASE/QUOTE:SETTLE` notation:

```python
from pycex import OKX

with OKX(api_key="KEY", secret="SECRET", passphrase="PASS", market_type="linear") as ex:
    funding = ex.fetch_funding_rate_sync("BTC/USDT:USDT")
    print(f"funding rate: {funding.rate:.6f} (next {funding.next_funding_time})")

    positions = ex.fetch_positions_sync()
    for p in positions:
        print(f"{p.symbol}: {p.side} {p.amount} @ {p.entry_price}, uPnL={p.unrealized_pnl}")

    order = ex.create_order_sync("BTC/USDT:USDT", "buy", "market", 1)
```

`fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on a `"spot"`
instance and on every KRW exchange (Upbit/Bithumb/Korbit are spot-only). See
`docs/api/exchanges.md` for per-venue quirks (contracts-vs-quantity on OKX,
hedge-mode caveats, etc.).

## Candles (OHLCV) and Pagination

```python
from pycex import Bybit

with Bybit() as ex:
    # Daily bars
    candles = ex.fetch_candles_sync("BTC/USDT", "1d", limit=30)

    # Pass since/until and pagination continues automatically (deduplicated, time-ordered)
    paged = ex.fetch_candles_sync("BTC/USDT", "1d", since=1_735_689_600_000, until=1_738_368_000_000)
```

Supported timeframes: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`

🚨 **The daily (1d) bar boundary differs by exchange** — `Candle.timestamp` is
always the bar open (UTC epoch ms), but where "today" starts depends on the
exchange:

| Exchange | Daily-bar boundary |
|----------|---------------------|
| Upbit | 00:00 UTC (= 09:00 KST) |
| Bithumb | 00:00 KST (= 15:00 UTC, previous day) |
| Korbit | 00:00 KST (= 15:00 UTC, previous day) |
| Binance | 00:00 UTC |
| OKX | 00:00 UTC |
| Bitget | 00:00 UTC |

Upbit and Bithumb return the same field name (`candle_date_time_utc`) but use
different daily boundaries, so the bar covering one KST calendar date can differ
between the two. See `tests/fixtures/NOTES.md` for the evidence.

### Candle pagination — measured per exchange

With `since`/`until`, `fetch_candles` pages automatically through ranges larger
than the exchange's per-request cap. Paging direction differs by exchange and
was set **by measurement, not documentation** (public endpoint probe,
2026-08-30):

| Exchange | Page cap | Direction | Bar time | Timeframes | Cursor parameter |
|----------|:---:|:---:|:---:|---|---|
| Binance (spot·linear) | 200 | forward | open | 1m 5m 15m 1h 4h 1d | `startTime` — from the **oldest** range |
| Bybit (spot·linear) | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `end` |
| OKX (spot·linear) | 100 | backward | open | 1m 5m 15m 1h 4h 1d | `after` |
| Bitget (spot·linear) | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `endTime` |
| Upbit | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `to` |
| Bithumb | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `to` |
| Korbit | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `end` |

- **backward** means the exchange's candle endpoint does not use `since` as a
  start; it returns the **most recent** bars before `until` (or "now"). So
  `fetch_candles` walks its cursor downward rather than upward
  (`BaseExchange.candle_paging`).
- Bybit and Bitget mix return the oldest side when sent only `start`, and the
  newest side when sent `start`+`end`. Range queries use the latter, so they
  walk backward. Bitget mix errors outright if both bounds span more than 90 days.
- Either way the result contract is the same: **deduplicated · ascending time ·
  inside `[since, until]` · up to `limit` bars**.
- Measured (2026-08-30, all 11 surfaces): 1h × 15 days = 360/360 bars,
  1d × 300 days = 300/300 bars. Reproduce with `pytest -m live tests/live -k pagination`.
- The numbers in the table match `pycex.constants.CANDLE_VENUES`. Timeframes not
  listed (`3m`, `1w`, ...) raise `NotSupportedError`.
- Bar time is the **open** on all seven. When an exchange gives the close time
  (Binance kline index 6), the adapter converts it to the open before setting
  `Candle.timestamp`.

### Closed bars

`Candle.timestamp` is the bar **open** (UTC epoch ms) on every exchange.
`closed_only=True` keeps a bar only when `timestamp + timeframe_ms <= now_ms`.

```python
from pycex import Binance

with Binance() as ex:
    closed = ex.fetch_candles_sync("BTC/USDT", "1m", limit=5, closed_only=True)
    history = ex.fetch_candles_history_sync("BTC/USDT", "1m", since=1_700_000_000_000)
```

`fetch_candles_history` is the async generator; the `_sync` twin returns a
`list`. With `until` omitted it stops at the last closed bar. An empty page
ends the walk. `RateLimitError` sleeps for `Retry-After` when the exchange
sends one, otherwise 1, 2, 4, 8, 16 seconds, at most five times.
`Market.listed_at` is the venue listing time, or `None` when the venue does
not publish one (usually `None` on Upbit, Bithumb, Korbit, Binance spot and
Bitget spot). Page limits and timeframes live in `CANDLE_VENUES`.

🚨 **Upbit and Bithumb read `to` in different time zones.** Upbit accepts a `Z`
suffix and reads naive values as **UTC**. Bithumb rejects any time zone suffix
(`Z` or `+00:00`) with HTTP 200 + `{"error":{"name":400,...}}` and reads naive
values as **KST**. The adapters format each one through the `_format_to` hook,
so callers only ever pass UTC epoch ms (`until=`).

🚨 **Bithumb also sends errors with HTTP 200** (`{"error":{"name":404,...}}`).
`KrwV1Mixin._check` catches this envelope regardless of status code and raises
`PyCexError` — both exchanges return `error.name` as the **integer** 404 for an
unknown market code, which becomes `SymbolNotFoundError`.

## Rate limits

Adapter defaults, published sources, and rows we could not confirm are in [docs/rate-limits.md](docs/rate-limits.md). Adopted values are at or below 80% of the confirmed published limit, rounded down. The 429 backoff in `fetch_candles_history` (1, 2, 4, 8, 16 seconds, at most five times) is unchanged.

## Supported Exchanges

| Exchange | spot | linear | sandbox | markets | candles+pagination | orders | balance | my_trades | positions | funding |
|----------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Binance  | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Bybit    | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ✕ | ✕ |
| OKX      | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Bitget   | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Upbit    | ○ | ✕ | ✕ | ○ | ○ | ○ | ○ | ○ | ✕ | ✕ |
| Bithumb  | ○ | ✕ | ✕ | ○ | ○ | ○ | ○ | ○ | ✕ | ✕ |
| Korbit   | ○ | ✕ | ✕ | ○ | ○ | ○ | ○ | ○ | ✕ | ✕ |

Bybit `linear`: markets/candles/orders/balance/my_trades all work under `category=linear`; positions/funding are not yet wired (out of phase-1 scope for this adapter) and raise `NotSupportedError` like a spot instance.

`candles+pagination` ○ means "a `since`/`until` request spanning more than one
page comes back complete"; the measured table above is the evidence.

`linear` = USDT-margined perpetual futures (`market_type="linear"`). Upbit,
Bithumb, and Korbit are KRW spot exchanges only — `sandbox=True` or
`market_type="linear"` raises `NotSupportedError` on all three;
`fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on every spot
instance (shared `BaseExchange` default).

Symbols use the same canonical notation (`BASE/QUOTE`, e.g. `BTC/USDT`) on
every exchange. The "Native Format" below is only what each adapter sends to the
exchange API internally; callers never use it directly.

| Exchange | Native Format (spot) | Native Format (linear) |
|----------|-----------------------|--------------------------|
| Binance | `BTCUSDT` | `BTCUSDT` |
| Bybit | `BTCUSDT` | `BTCUSDT` |
| OKX | `BTC-USDT` | `BTC-USDT-SWAP` |
| Bitget | `BTCUSDT` | `BTCUSDT` |
| Upbit | `KRW-BTC` | — |
| Bithumb | `KRW-BTC` | — |
| Korbit | `btc_krw` | — |

## Raw API Response

Every model exposes the original exchange response through `.raw`:

```python
from pycex import Binance

with Binance() as ex:
    ticker = ex.fetch_ticker_sync("BTC/USDT")
    print(ticker.raw)  # Original Binance API response dict
```

## CLI

```bash
export PYCEX_EXCHANGE=binance
export PYCEX_API_KEY="your_key"
export PYCEX_SECRET="your_secret"

pycex ticker BTC/USDT              # ticker
pycex orderbook BTC/USDT           # order book
pycex balance                      # balance
pycex buy BTC/USDT 0.001 50000     # buy
pycex sell BTC/USDT 0.001 55000    # sell

# Choose the exchange (binance, bybit, okx, bitget, upbit, bithumb, korbit)
pycex -e bybit ticker BTC/USDT
pycex -e okx ticker BTC/USDT
pycex -e upbit ticker BTC/KRW

# Sandbox + linear market type + JSON
pycex --sandbox --market-type linear -e okx --json ticker BTC/USDT:USDT
```

See `docs/cli.md` for the full command reference.

## MCP Server (Claude Desktop)

`claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "crypto": {
            "command": "pycex-mcp",
            "env": {
                "PYCEX_EXCHANGE": "binance",
                "PYCEX_API_KEY": "your_key",
                "PYCEX_SECRET": "your_secret"
            }
        }
    }
}
```

See `docs/mcp.md` for the full tool list (ticker/order book/balance/orders,
plus multi-exchange `compare_prices`/`aggregate_balance` and chart-analysis
tools) and prompt reference.

## Environment Variables

Credentials are referenced only by environment variable name — never put the
values in code or logs. Generic and per-exchange variables are both supported,
and the per-exchange variable wins when both are set (shared by the CLI and MCP
through `pycex.factory.create_exchange`):

| Variable | Description |
|----------|-------------|
| `PYCEX_EXCHANGE` | Exchange name (`binance`, `bybit`, `okx`, `bitget`, `upbit`, `bithumb`, `korbit`) |
| `PYCEX_API_KEY` / `PYCEX_{EXCHANGE}_API_KEY` | API key (e.g. `PYCEX_BYBIT_API_KEY`) |
| `PYCEX_SECRET` / `PYCEX_{EXCHANGE}_SECRET` | API secret |
| `PYCEX_PASSPHRASE` / `PYCEX_{EXCHANGE}_PASSPHRASE` | OKX/Bitget passphrase |
| `PYCEX_SANDBOX` | `1`/`true` — use sandbox/demo/testnet |
| `PYCEX_MARKET_TYPE` | `spot` (default) or `linear` |
| `PYCEX_TESTNET` | **Deprecated** — old name for `PYCEX_SANDBOX` (still works) |

## License

MIT

# pycex

Unified Python wrapper for cryptocurrency exchanges — **Binance**, **Bybit**,
**OKX**, **Bitget**, **Upbit**, **Bithumb**, **Korbit**.

ccxt의 복잡한 코드베이스 대신, Pydantic 타입 안전성과 async-first 설계, 내장 rate limiter, MCP 서버를 제공하는 깔끔한 Python 라이브러리입니다.

## Features

- **Unified API** — 동일한 인터페이스로 7개 거래소 사용 (spot + USDT-margined perpetual)
- **Sync + Async** — 동기/비동기 모두 지원 (매 메서드마다 자동 생성된 `_sync` 트윈)
- **Canonical Symbols** — 모든 거래소에서 동일한 `BASE/QUOTE`(spot) / `BASE/QUOTE:SETTLE`(linear) 표기
- **Pydantic Models** — `Ticker`, `OrderBook`, `Balance`, `Order`, `Candle`, `Trade`, `Market`, `MyTrade`, `Position`, `FundingRate`
- **Rate Limiting** — Token bucket rate limiter 내장 (거래소별 기본값)
- **MCP Server** — Claude Desktop 등 AI 어시스턴트 연동
- **CLI** — 터미널에서 시세, 잔고, 주문
- **Type Safe** — PEP 561 `py.typed`, 완전한 타입 힌트, `mypy --strict` 통과

## Installation

```bash
pip install pycex

# MCP 서버 사용 시
pip install pycex[mcp]
```

## Quick Start

심볼은 거래소와 무관하게 항상 정규(canonical) 표기를 씁니다 — spot은
`BASE/QUOTE`(예: `BTC/USDT`), USDT 무기한 선물은 `BASE/QUOTE:SETTLE`(예:
`BTC/USDT:USDT`). 각 어댑터가 내부적으로 거래소별 네이티브 표기(`BTCUSDT`,
`BTC-USDT`, `KRW-BTC` 등)로 변환하므로, 네이티브 표기를 직접 넘기면
`SymbolNotFoundError`가 발생합니다.

```python
from pycex import Binance

# 시세 조회 (인증 불필요)
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

동일한 인터페이스로 거래소를 교체할 수 있습니다:

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
    # 잔고 조회
    balance = ex.fetch_balance_sync()
    btc = balance.get("BTC")
    if btc:
        print(f"BTC: free={btc.free}, locked={btc.locked}, total={btc.total}")

    # Limit 매수
    order = ex.create_order_sync("BTC/USDT", "buy", "limit", amount=0.001, price=50000.0)
    print(f"Order placed: {order.id}")

    # 주문 취소
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
    # 일봉
    candles = ex.fetch_candles_sync("BTC/USDT", "1d", limit=30)

    # since/until 를 주면 페이지네이션이 자동으로 이어집니다 (중복 제거, 시간순 정렬)
    paged = ex.fetch_candles_sync("BTC/USDT", "1d", since=1_735_689_600_000, until=1_738_368_000_000)
```

지원 timeframe: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`

🚨 **일봉(1d) 경계가 거래소마다 다릅니다** — `Candle.timestamp`는 항상 봉 시작
시각(UTC epoch ms)이지만, "오늘"이 어디서 끊기는지는 거래소별로 다릅니다:

| Exchange | Daily-bar boundary |
|----------|---------------------|
| Upbit | 00:00 UTC (= 09:00 KST) |
| Bithumb | 00:00 KST (= 15:00 UTC, 전날) |
| Korbit | 00:00 KST (= 15:00 UTC, 전날) |
| Binance | 00:00 UTC |
| OKX | 00:00 UTC |
| Bitget | 00:00 UTC |

같은 필드 이름(`candle_date_time_utc`)을 반환해도 Upbit과 Bithumb의 일봉 경계는
다르므로, KST 캘린더 날짜 하나에 대응하는 원장 봉이 두 거래소에서 서로 다를 수
있습니다. 자세한 근거는 `tests/fixtures/NOTES.md`를 참고하세요.

### 캔들 페이지네이션 — 거래소별 실측

`since`/`until` 을 주면 `fetch_candles` 가 거래소 상한을 넘는 구간을 자동으로
페이징합니다. 페이징 방향은 거래소마다 다르고, **문서가 아니라 실측으로**
정했습니다 (2026-08-30 공개 엔드포인트 프로브):

| Exchange | 1페이지 상한 | 페이징 방향 | 봉 시각 | timeframe | 커서 파라미터 |
|----------|:---:|:---:|:---:|---|---|
| Binance (spot·linear) | 200 | forward | open | 1m 5m 15m 1h 4h 1d | `startTime` — 가장 **오래된** 구간부터 |
| Bybit (spot·linear) | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `end` |
| OKX (spot·linear) | 100 | backward | open | 1m 5m 15m 1h 4h 1d | `after` |
| Bitget (spot·linear) | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `endTime` |
| Upbit | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `to` |
| Bithumb | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `to` |
| Korbit | 200 | backward | open | 1m 5m 15m 1h 4h 1d | `end` |

- **backward** = 그 거래소의 캔들 엔드포인트는 `since` 를 시작점으로 쓰지 않고
  `until`(없으면 "지금") 기준으로 **가장 최근** 봉부터 되돌려줍니다. 그래서
  `fetch_candles` 는 커서를 위가 아니라 아래로 내리며 걷습니다
  (`BaseExchange.candle_paging`).
- Bybit·Bitget mix 는 `start` 만 보내면 오래된 쪽을, `start`+`end` 를 같이
  보내면 최신 쪽을 돌려줍니다. 구간 조회는 후자이므로 backward 로 걷습니다.
  Bitget mix 는 두 경계를 90일 넘게 같이 보내면 아예 오류입니다.
- 어느 방향이든 결과 계약은 같습니다: **중복 제거 · 시간 오름차순 ·
  `[since, until]` 안 · `limit` 개까지**.
- 실측 결과(2026-08-30, 11개 surface 전부): 1h × 15일 = 360/360봉,
  1d × 300일 = 300/300봉. 재현은 `pytest -m live tests/live -k pagination`.
- 위 표의 숫자는 `pycex.constants.CANDLE_VENUES` 와 같다. 표에 없는
  timeframe(`3m`, `1w` 등)은 `NotSupportedError`.
- 봉 시각은 일곱 곳 모두 **시작(open)**. 거래소가 종료 시각을 주는 경우(Binance
  kline index 6)도 어댑터가 시작으로 맞춘 뒤 `Candle.timestamp`에 넣는다.

### 닫힌 봉과 소급

`closed_only=True` 이면 아직 끝나지 않은 봉을 빼니다. 판정은
`timestamp + timeframe_ms <= now_ms` (봉 종료 ≤ 지금)입니다.

```python
from pycex import Binance

with Binance() as ex:
    closed = ex.fetch_candles_sync("BTC/USDT", "1m", limit=5, closed_only=True)

    # since 부터 마지막 닫힌 봉까지. 페이지 상한·빈 응답·429 백오프(최대 5회)는 도우미가 처리합니다.
    history = ex.fetch_candles_history_sync("BTC/USDT", "1m", since=1_700_000_000_000)
```

`fetch_markets()` 의 `Market.listed_at` 은 거래소가 주는 상장 시각(UTC)이고,
없으면 `None` 입니다 (업비트·빗썸·코빗·바이낸스 현물·비트겟 현물은 보통 `None`).

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
not publish one. Page limits and timeframes live in `CANDLE_VENUES`.

🚨 **Upbit·Bithumb 의 `to` 는 타임존 해석이 다릅니다.** Upbit 은 `Z` 접미사를
받고 naive 값을 **UTC** 로 읽습니다. Bithumb 은 타임존 접미사가 붙으면 (`Z`,
`+00:00` 모두) HTTP 200 + `{"error":{"name":400,...}}` 로 거부하고, naive 값을
**KST** 로 읽습니다. 어댑터가 `_format_to` 훅으로 각각 맞춰 보내므로 호출부는
언제나 UTC epoch ms(`until=`) 만 주면 됩니다.

🚨 **Bithumb 은 오류도 HTTP 200 으로 보냅니다** (`{"error":{"name":404,...}}`).
`KrwV1Mixin._check` 가 상태 코드와 무관하게 이 봉투를 잡아 `PyCexError` 로
올립니다 — 두 거래소 모두 알 수 없는 마켓 코드에 `error.name` 을 **정수** 404
로 돌려주므로 `SymbolNotFoundError` 가 됩니다.

## Rate limits

어댑터 기본값은 거래소 공시 한도의 80%를 정수로 내린 값입니다. OKX 주문과, 공시를 확인하지 못한 주문 버킷은 이전 값(초당 5회)을 유지합니다. `fetch_candles_history`의 429 백오프(1, 2, 4, 8, 16초, 최대 5회)는 바꾸지 않았습니다. 빈 concurrency 칸은 쿼리 동시성 상한을 따로 두지 않는다는 뜻입니다.

OKX 최근 캔들(`/market/candles`, 공시 40/2s)은 가중치 0.5, 히스토리 캔들(공시 20/2s)은 가중치 1입니다. 공유 query 버킷 16/2s에서 각각 32/2s, 16/2s가 되어 둘 다 공시의 80%입니다.

| exchange | market | bucket | limit | period_s | concurrency |
|---|---|---|---|---|---|
| binance | spot | total | 4800 | 60 | |
| binance | spot | order | 80 | 10 | |
| binance | linear | total | 1920 | 60 | |
| binance | linear | order | 240 | 10 | |
| binance | linear | order_minute | 960 | 60 | |
| bybit | spot | query | 480 | 5 | 4 |
| bybit | spot | order | 16 | 1 | 4 |
| bybit | linear | query | 480 | 5 | 4 |
| bybit | linear | order | 8 | 1 | 4 |
| bybit | spot | private | 40 | 1 | 4 |
| bybit | linear | private | 40 | 1 | 4 |
| okx | spot | query | 16 | 2 | 4 |
| okx | spot | order | 5 | 1 | 4 |
| bitget | spot | query | 16 | 1 | 4 |
| bitget | spot | order | 5 | 1 | 4 |
| upbit | spot | query | 24 | 1 | |
| upbit | spot | order | 9 | 1 | |
| bithumb | spot | query | 120 | 1 | 4 |
| bithumb | spot | order | 8 | 1 | 4 |
| korbit | spot | query | 40 | 1 | 4 |
| korbit | spot | order | 24 | 1 | 4 |

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

`candles+pagination` ○ 는 "`since`/`until` 로 한 페이지를 넘는 구간을 요청하면
빠짐없이 돌려준다"는 뜻이고, 위 실측 표가 그 근거입니다.

`linear` = USDT-margined perpetual futures (`market_type="linear"`). Upbit,
Bithumb, and Korbit are KRW spot exchanges only — `sandbox=True` or
`market_type="linear"` raises `NotSupportedError` on all three;
`fetch_positions`/`fetch_funding_rate` raise `NotSupportedError` on every spot
instance (shared `BaseExchange` default).

심볼은 모든 거래소에서 동일하게 표준 표기(`BASE/QUOTE`, 예: `BTC/USDT`)를 씁니다 —
아래 "Native Format"은 각 어댑터가 내부적으로 거래소 API에 보내는 표기일 뿐,
호출부에서 직접 쓰지 않습니다.

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

모든 모델은 `.raw` 필드로 원본 거래소 응답에 접근할 수 있습니다:

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

pycex ticker BTC/USDT              # 시세 조회
pycex orderbook BTC/USDT           # 호가창
pycex balance                      # 잔고
pycex buy BTC/USDT 0.001 50000     # 매수
pycex sell BTC/USDT 0.001 55000    # 매도

# 거래소 지정 (binance, bybit, okx, bitget, upbit, bithumb, korbit)
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

모든 인증 값은 환경변수 이름으로만 지칭됩니다 — 값을 코드나 로그에 쓰지 마세요.
일반(generic) 변수와 거래소별(per-exchange) 변수가 함께 지원되며, 거래소별
변수가 있으면 그쪽이 우선합니다 (CLI/MCP 공용 `pycex.factory.create_exchange`):

| Variable | Description |
|----------|-------------|
| `PYCEX_EXCHANGE` | 거래소 이름 (`binance`, `bybit`, `okx`, `bitget`, `upbit`, `bithumb`, `korbit`) |
| `PYCEX_API_KEY` / `PYCEX_{EXCHANGE}_API_KEY` | API 키 (예: `PYCEX_BYBIT_API_KEY`) |
| `PYCEX_SECRET` / `PYCEX_{EXCHANGE}_SECRET` | API 시크릿 |
| `PYCEX_PASSPHRASE` / `PYCEX_{EXCHANGE}_PASSPHRASE` | OKX/Bitget passphrase |
| `PYCEX_SANDBOX` | `1`/`true` — 샌드박스/데모/테스트넷 사용 |
| `PYCEX_MARKET_TYPE` | `spot`(기본) 또는 `linear` |
| `PYCEX_TESTNET` | **Deprecated** — `PYCEX_SANDBOX`의 예전 이름 (여전히 동작) |

## License

MIT

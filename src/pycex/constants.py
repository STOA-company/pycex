"""Constants for exchange URLs and rate limits."""

from __future__ import annotations

from typing import Literal, NamedTuple

# ── Binance ──
BINANCE_BASE = "https://api.binance.com"
BINANCE_TESTNET = "https://testnet.binance.vision"
BINANCE_FAPI = "https://fapi.binance.com"  # USDT-M perpetual futures (market_type="linear")
BINANCE_FAPI_TESTNET = "https://testnet.binancefuture.com"

# ── Exchange request budgets ──
#
# Each count is floor(published cap * 0.8). The README «Rate limits» table is
# the same list the tests literal-compare. 429 backoff is not part of this table.
#
# Upbit: https://docs.upbit.com/kr/reference/rate-limits
# Quotation groups including candles are 10/s per IP. Exchange `default` (private
# queries) is 30/s per pocket. Order create is 12/s per pocket.
UPBIT_QUERY_RATE_LIMIT = (24, 1.0)
UPBIT_ORDER_RATE_LIMIT = (9, 1.0)
UPBIT_PUBLIC_RATE_LIMIT = (8, 1.0)

# Binance spot weight 6000/min and orders 100/10s; USD-M weight 2400/min,
# orders 300/10s and 1200/min. Read from exchangeInfo on 2026-09-22.
# Spot klines weight 2: https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
BINANCE_SPOT_WEIGHT_RATE_LIMIT = (4800, 60.0)
BINANCE_SPOT_ORDER_RATE_LIMIT = (80, 10.0)
BINANCE_LINEAR_WEIGHT_RATE_LIMIT = (1920, 60.0)
BINANCE_LINEAR_ORDER_RATE_LIMIT = (240, 10.0)
BINANCE_LINEAR_ORDER_MINUTE_RATE_LIMIT = (960, 60.0)

# Bybit IP limit 600/5s. Spot create 20/s, linear create 10/s.
# Wallet, open orders, and executions are 50/s.
# https://bybit-exchange.github.io/docs/v5/rate-limit
BYBIT_IP_RATE_LIMIT = (480, 5.0)
BYBIT_SPOT_ORDER_RATE_LIMIT = (16, 1.0)
BYBIT_LINEAR_ORDER_RATE_LIMIT = (8, 1.0)
BYBIT_PRIVATE_QUERY_RATE_LIMIT = (40, 1.0)

# Query bucket is 16/2s. Recent candles use weight 0.5; history candles use 1.
# The order bucket stays at the previous (5, 1.0). Raising it is a separate
# change: OKX is a live-account venue.
# https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks
OKX_QUERY_RATE_LIMIT = (16, 2.0)
OKX_ORDER_RATE_LIMIT = (5, 1.0)
OKX_RECENT_CANDLE_WEIGHT = 0.5
OKX_HISTORY_CANDLE_WEIGHT = 1.0

# Bitget public market data 20/s.
# https://www.bitget.com/api-doc/classic/contract/market/Get-Candle-Data
# The order-endpoint cap was not confirmed, so the order bucket stays 5/s.
BITGET_PUBLIC_RATE_LIMIT = (16, 1.0)
BITGET_ORDER_RATE_LIMIT = (5, 1.0)

# Bithumb public 150/s; order calls may be limited above 10/s.
# https://apidocs.bithumb.com/docs/api-%EC%9A%94%EC%B2%AD-%EC%88%98-%EC%A0%9C%ED%95%9C-%EC%95%88%EB%82%B4
BITHUMB_PUBLIC_RATE_LIMIT = (120, 1.0)
BITHUMB_ORDER_RATE_LIMIT = (8, 1.0)

# Korbit public 50/s, orders 30/s. api.korbit.co.kr still serves this API.
# https://docs.korbit.co.kr/
KORBIT_PUBLIC_RATE_LIMIT = (40, 1.0)
KORBIT_ORDER_RATE_LIMIT = (24, 1.0)

# Published limits are request rates, so these venues may keep several calls in flight.
DEFAULT_MAX_INFLIGHT = 4

# ── Bybit ──
BYBIT_BASE = "https://api.bybit.com"
BYBIT_TESTNET = "https://api-testnet.bybit.com"

# ── OKX ──
OKX_BASE = "https://www.okx.com"
OKX_DEMO = "https://www.okx.com"  # same host, demo flag in header

# ── Bitget ──
BITGET_BASE = "https://api.bitget.com"  # same host for live and demo, demo via paptrading header

# ── Upbit ──
UPBIT_BASE = "https://api.upbit.com"  # no sandbox/demo environment

# ── Bithumb ──
BITHUMB_BASE = "https://api.bithumb.com"  # no sandbox/demo environment; v1/v2 REST paths coexist

# ── Korbit ──
KORBIT_BASE = "https://api.korbit.co.kr"  # no sandbox/demo environment

# ── Timeframes ──
# Canonical timeframe -> bar duration in milliseconds. Single source for every
# adapter and for BaseExchange's backward-paging anchor.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Page limit, paging direction, and bar-open contract. Adapters read this table;
# README «Candles (OHLCV) and Pagination» repeats it. Every venue's candle
# timestamp is the bar open (UTC epoch ms), already normalized in the parser.
_CANDLE_TIMEFRAMES = frozenset(TIMEFRAME_MS)


class CandleVenue(NamedTuple):
    page_limit: int
    paging: Literal["forward", "backward"]
    bar_time: str
    timeframes: frozenset[str]


CANDLE_VENUES: dict[str, CandleVenue] = {
    "binance": CandleVenue(200, "forward", "open", _CANDLE_TIMEFRAMES),
    "bybit": CandleVenue(200, "backward", "open", _CANDLE_TIMEFRAMES),
    "okx": CandleVenue(100, "backward", "open", _CANDLE_TIMEFRAMES),
    "bitget": CandleVenue(200, "backward", "open", _CANDLE_TIMEFRAMES),
    "upbit": CandleVenue(200, "backward", "open", _CANDLE_TIMEFRAMES),
    "bithumb": CandleVenue(200, "backward", "open", _CANDLE_TIMEFRAMES),
    "korbit": CandleVenue(200, "backward", "open", _CANDLE_TIMEFRAMES),
}

# ── Sides ──
BUY = "buy"
SELL = "sell"

# ── Order types ──
LIMIT = "limit"
MARKET = "market"

# ── Broker / Referral IDs ──
# These are sent with every API request for affiliate attribution.
# Apply at each exchange's broker/partner program to get your own IDs.
BINANCE_BROKER_ID = ""
BYBIT_REFERRAL_CODE = ""
OKX_BROKER_ID = ""
BITGET_BROKER_ID = ""  # X-CHANNEL-API-CODE for API broker rebate

# ── Symbol resolution ──
# Quote-asset suffixes tried (longest-match-first is not required here since
# each candidate is checked in this fixed priority order) when an adapter has
# no markets cache yet to resolve a native symbol back to canonical notation.
QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "BTC", "ETH", "BNB", "FDUSD", "TRY", "EUR", "KRW")

# ── Timeframes ──
TIMEFRAME_1m = "1m"
TIMEFRAME_5m = "5m"
TIMEFRAME_15m = "15m"
TIMEFRAME_1h = "1h"
TIMEFRAME_4h = "4h"
TIMEFRAME_1d = "1d"
TIMEFRAME_1w = "1w"

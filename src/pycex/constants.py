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
# Upbit: https://docs.upbit.com/kr/reference/rate-limits
# The live document currently lists orders as 12/s.  X8 deliberately keeps the
# lower 8/s value below as a safety margin while the execution layer rolls out.
UPBIT_QUERY_RATE_LIMIT = (30, 1.0)
UPBIT_ORDER_RATE_LIMIT = (8, 1.0)
UPBIT_PUBLIC_RATE_LIMIT = (10, 1.0)

# Binance spot: https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/rate-limits
# Binance USD-M market data: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
# Binance USD-M order quota: https://www.binance.com/en/support/announcement/detail/6bc47f8b8a05445cb07b30454fec4084
# Quotas also read directly on 2026-09-12 (some documentation examples are older):
# https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT
# https://fapi.binance.com/fapi/v1/exchangeInfo
BINANCE_SPOT_WEIGHT_RATE_LIMIT = (6000, 60.0)
BINANCE_SPOT_ORDER_RATE_LIMIT = (100, 10.0)
BINANCE_LINEAR_WEIGHT_RATE_LIMIT = (2400, 60.0)
BINANCE_LINEAR_ORDER_RATE_LIMIT = (300, 10.0)

# Bithumb, Korbit, Bitget, and OKX: 문서 재확인 필요
# Until each venue's current endpoint-specific rules are rechecked, retain a
# deliberately small per-class and shared allowance plus one in-flight call.
CONSERVATIVE_RATE_LIMIT = (5, 1.0)
CONSERVATIVE_MAX_INFLIGHT = 1

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

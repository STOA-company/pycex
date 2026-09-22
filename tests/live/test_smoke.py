"""Live smoke tests over the 11 exchange/market-type surfaces.

These hit real public (and, when credentials are present, private) exchange
endpoints. They are excluded from the default test run (see ``addopts`` in
``pyproject.toml``) and only run via ``pytest -m live tests/live``.
"""

from __future__ import annotations

import os
import time

import pytest

from pycex.exceptions import PyCexError
from pycex.factory import create_exchange
from pycex.models import Balance

CASES = [
    ("upbit", "spot", "BTC/KRW"),
    ("bybit", "spot", "BTC/USDT"),
    ("bybit", "linear", "BTC/USDT:USDT"),
    ("bithumb", "spot", "BTC/KRW"),
    ("korbit", "spot", "BTC/KRW"),
    ("binance", "spot", "BTC/USDT"),
    ("okx", "spot", "BTC/USDT"),
    ("bitget", "spot", "BTC/USDT"),
    ("binance", "linear", "BTC/USDT:USDT"),
    ("okx", "linear", "BTC/USDT:USDT"),
    ("bitget", "linear", "BTC/USDT:USDT"),
]

# KRW exchanges that reset their daily candle at KST midnight (= 15:00 UTC the
# previous day) instead of UTC midnight. Upbit and the three global venues
# reset at UTC midnight. See tests/fixtures/NOTES.md and docs/api/exchanges.md.
_KST_MIDNIGHT_BOUNDARY = {"bithumb", "korbit"}

_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000

# (name, market_type, symbol, timeframe, span_ms, bar_ms) for the multi-page
# pagination path (since=/until= over BaseExchange.fetch_candles).
#
# 🚨 Every span here is deliberately LONGER than one page on the venue it runs
# against — 360 bars against a candle_page_limit of 200 (OKX: 100), 300 bars for
# the 1d case. The previous version of this test asked for a 10-day span of 1d
# bars, which fits in a single page on every venue, so it passed while five of
# the seven adapters truncated every real multi-page range (live 2026-08-30,
# 500 daily bars requested: upbit 200, korbit 200, okx 100, bybit 199/200).
PAGINATION_CASES = [
    ("upbit", "spot", "BTC/KRW", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("korbit", "spot", "BTC/KRW", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("binance", "spot", "BTC/USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("okx", "spot", "BTC/USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("bitget", "spot", "BTC/USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("bybit", "spot", "BTC/USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("binance", "linear", "BTC/USDT:USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("okx", "linear", "BTC/USDT:USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("bitget", "linear", "BTC/USDT:USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("bybit", "linear", "BTC/USDT:USDT", "1h", 15 * _DAY_MS, _HOUR_MS),
    ("bithumb", "spot", "BTC/KRW", "1d", 300 * _DAY_MS, _DAY_MS),
]

# Spot BTC on each of the seven venues. 1m bars share a UTC minute boundary
# (daily boundaries differ; minute bars do not).
CLOSED_ONLY_CASES = [
    ("binance", "spot", "BTC/USDT"),
    ("bybit", "spot", "BTC/USDT"),
    ("okx", "spot", "BTC/USDT"),
    ("bitget", "spot", "BTC/USDT"),
    ("upbit", "spot", "BTC/KRW"),
    ("bithumb", "spot", "BTC/KRW"),
    ("korbit", "spot", "BTC/KRW"),
]

# Venues whose public market-data surface answers an unknown market with the
# KRW-v1 error envelope (Bithumb serves it with HTTP 200 — see KrwV1Mixin._check).
UNKNOWN_SYMBOL_CASES = [("upbit", "NOPE/KRW"), ("bithumb", "NOPE/KRW")]


@pytest.mark.live
@pytest.mark.parametrize("name,mt,symbol", CASES)
async def test_public_surface(name: str, mt: str, symbol: str) -> None:
    async with create_exchange(name, market_type=mt) as ex:
        markets = await ex.fetch_markets()
        assert any(m.symbol == symbol for m in markets), f"{symbol} missing from {name} markets"

        candles = await ex.fetch_candles(symbol, "1d", limit=5)
        assert 1 <= len(candles) <= 5
        expected_offset = 15 * 3600 * 1000 if name in _KST_MIDNIGHT_BOUNDARY else 0
        assert all(c.timestamp % 86_400_000 == expected_offset for c in candles), (
            f"{name} 1d candles must start at expected day boundary (offset={expected_offset}ms)"
        )

        ticker = await ex.fetch_ticker(symbol)
        assert ticker.last > 0

        ob = await ex.fetch_order_book(symbol)
        assert len(ob.bids) >= 1
        assert len(ob.asks) >= 1

        trades = await ex.fetch_trades(symbol, limit=5)
        assert 1 <= len(trades) <= 5


@pytest.mark.live
@pytest.mark.parametrize("name,mt,symbol", CASES)
async def test_authenticated_smoke(name: str, mt: str, symbol: str) -> None:
    env_key = f"PYCEX_{name.upper()}_API_KEY"
    if not os.environ.get(env_key):
        pytest.skip(f"{env_key} not set")
    async with create_exchange(name, market_type=mt) as ex:
        balance = await ex.fetch_balance()
        assert isinstance(balance, Balance)


@pytest.mark.live
@pytest.mark.parametrize("name,mt,symbol,timeframe,span_ms,bar_ms", PAGINATION_CASES)
async def test_candle_pagination(name: str, mt: str, symbol: str, timeframe: str, span_ms: int, bar_ms: int) -> None:
    until = int(time.time() * 1000)
    since = until - span_ms
    expected = span_ms // bar_ms
    async with create_exchange(name, market_type=mt) as ex:
        assert span_ms // bar_ms > ex.candle_page_limit, "span must exceed one page or this proves nothing"
        candles = await ex.fetch_candles(symbol, timeframe, since=since, until=until)
        # ±2 absorbs the partially-formed bar at each end of an arbitrary wall-clock span.
        assert abs(len(candles) - expected) <= 2, f"{name}/{mt}: got {len(candles)} bars, expected ~{expected}"
        timestamps = [c.timestamp for c in candles]
        assert timestamps == sorted(timestamps), "candles must be ascending"
        assert len(timestamps) == len(set(timestamps)), "candles must have no duplicate timestamps"
        assert all(b - a > 0 for a, b in zip(timestamps, timestamps[1:])), "candles must be strictly ascending"
        assert all(since <= t <= until for t in timestamps), "candles must stay inside [since, until]"


@pytest.mark.live
@pytest.mark.parametrize("name,mt,symbol", CLOSED_ONLY_CASES)
async def test_closed_only_last_1m_bar(name: str, mt: str, symbol: str) -> None:
    """Public, unauthenticated. The last 1m bar must already have ended."""
    now_ms = int(time.time() * 1000)
    async with create_exchange(name, market_type=mt) as ex:
        markets = await ex.fetch_markets()
        assert len(markets) >= 1
        assert any(m.symbol == symbol for m in markets)
        candles = await ex.fetch_candles(symbol, "1m", limit=5, closed_only=True)
        assert candles, f"{name} returned no closed 1m bars"
        assert all(c.timestamp + 60_000 <= now_ms for c in candles)
        assert candles[-1].timestamp + 60_000 <= int(time.time() * 1000)


@pytest.mark.live
@pytest.mark.parametrize("name,symbol", UNKNOWN_SYMBOL_CASES)
async def test_unknown_symbol_raises_pycex_error(name: str, symbol: str) -> None:
    """Both KRW venues used to answer this with a TypeError (Upbit: int `error.name`)
    or a KeyError (Bithumb: error envelope served with HTTP 200)."""
    async with create_exchange(name) as ex:
        with pytest.raises(PyCexError):
            await ex.fetch_ticker(symbol)

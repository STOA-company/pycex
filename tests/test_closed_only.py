"""F1 — closed_only drops a bar whose end is still in the future.

A bar is closed when ``timestamp + timeframe_ms <= now_ms``. Every adapter
already stores the bar OPEN in ``Candle.timestamp``; these tests pin that
field to a literal so a parser that switches to close-time (or a fixture
whose open time changes) goes red.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from pycex.base import BaseExchange
from pycex.exceptions import NotSupportedError
from pycex.models import Candle
from tests.conftest import load_fixture

_TF = 60_000
_NOW_MS = 180_000


class _Clock(BaseExchange):
    name = "clock"
    candle_page_limit = 10

    def __init__(self, bars: list[int]) -> None:
        self._bars = bars

    def to_native(self, symbol: str) -> str:
        return symbol

    def from_native(self, native: str) -> str:
        return native

    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        return [_bar(t) for t in self._bars[:limit]]

    async def fetch_ticker(self, symbol):
        raise NotSupportedError

    async def fetch_order_book(self, symbol, *, limit=20):
        raise NotSupportedError

    async def fetch_trades(self, symbol, *, limit=100):
        raise NotSupportedError

    async def fetch_markets(self):
        raise NotSupportedError

    async def fetch_balance(self):
        raise NotSupportedError

    async def create_order(self, symbol, side, order_type, amount, price=None):
        raise NotSupportedError

    async def cancel_order(self, order_id, symbol):
        raise NotSupportedError

    async def fetch_order(self, order_id, symbol):
        raise NotSupportedError

    async def fetch_open_orders(self, symbol=None):
        raise NotSupportedError

    async def fetch_my_trades(self, symbol=None, *, since=None, limit=None):
        raise NotSupportedError


def _bar(ts: int) -> Candle:
    return Candle(timestamp=ts, open=1, high=1, low=1, close=1, volume=1)


def _freeze(monkeypatch: pytest.MonkeyPatch, now_ms: int = _NOW_MS) -> None:
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)


async def test_closed_only_drops_the_bar_that_has_not_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bars at 0, 60s, 120s are closed at t=180s; the bar that opens at 180s is not."""
    _freeze(monkeypatch)
    ex = _Clock([0, _TF, 2 * _TF, 3 * _TF])
    out = await ex.fetch_candles("BTC/USDT", "1m", closed_only=True)
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF]


async def test_closed_only_keeps_a_bar_that_ends_exactly_now(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, now_ms=2 * _TF)
    ex = _Clock([0, _TF])
    out = await ex.fetch_candles("BTC/USDT", "1m", closed_only=True)
    assert [c.timestamp for c in out] == [0, _TF]


async def test_closed_only_false_keeps_the_forming_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch)
    ex = _Clock([0, _TF, 2 * _TF, 3 * _TF])
    out = await ex.fetch_candles("BTC/USDT", "1m")
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF, 3 * _TF]


def test_closed_only_sync_twin_uses_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch)
    ex = _Clock([0, _TF, 2 * _TF, 3 * _TF])
    out = ex.fetch_candles_sync("BTC/USDT", "1m", closed_only=True)
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF]


def _utc_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_binance_candle_timestamp_is_open_not_close() -> None:
    from pycex.exchanges.binance import _parse_candle

    raw = load_fixture("binance", "candles_linear_1d")[0]
    assert raw[0] == 1787875200000
    assert raw[6] == 1787961599999
    candle = _parse_candle(raw)
    assert candle.timestamp == 1787875200000
    assert candle.timestamp != 1787961599999


def test_bybit_candle_timestamp_is_open_not_price() -> None:
    from pycex.exchanges.bybit import _parse_candle

    # No recorded Bybit candle fixture. Shape matches tests/exchanges/test_bybit.py
    # (start, open, high, low, close, volume, turnover); index 0 is the open.
    raw = ["1787875200000", "80208.90", "81500.00", "76853.10", "77805.90", "1", "1"]
    candle = _parse_candle(raw)
    assert candle.timestamp == 1787875200000
    assert candle.timestamp != int(float(raw[1]))


def test_okx_candle_timestamp_is_open() -> None:
    from pycex.exchanges.okx import _parse_candle

    raw = load_fixture("okx", "candles_swap_1d")["data"][0]
    assert raw[0] == "1788048000000"
    candle = _parse_candle(raw)
    assert candle.timestamp == 1788048000000
    assert candle.timestamp != int(float(raw[1]))


def test_bitget_candle_timestamp_is_open() -> None:
    from pycex.exchanges.bitget import _parse_candle

    raw = load_fixture("bitget", "candles_linear_1d")["data"][0]
    assert raw[0] == "1787875200000"
    candle = _parse_candle(raw)
    assert candle.timestamp == 1787875200000
    assert candle.timestamp != int(float(raw[1]))


def test_upbit_candle_timestamp_is_open_not_last_tick() -> None:
    from pycex.exchanges.upbit import _parse_candle

    raw = load_fixture("upbit", "candles_1d")[0]
    assert raw["candle_date_time_utc"] == "2026-08-30T00:00:00"
    assert raw["timestamp"] == 1788063995383
    candle = _parse_candle(raw)
    assert candle.timestamp == 1788048000000
    assert candle.timestamp == _utc_ms("2026-08-30T00:00:00")
    assert candle.timestamp != 1788063995383


def test_bithumb_candle_timestamp_is_open_not_last_tick() -> None:
    from pycex.exchanges.bithumb import _parse_candle

    raw = load_fixture("bithumb", "candles_1d")[0]
    assert raw["candle_date_time_utc"] == "2026-08-29T15:00:00"
    assert raw["timestamp"] == 1788063973000
    candle = _parse_candle(raw)
    assert candle.timestamp == 1788015600000
    assert candle.timestamp == _utc_ms("2026-08-29T15:00:00")
    assert candle.timestamp != 1788063973000


def test_korbit_candle_timestamp_is_the_open_field() -> None:
    from pycex.exchanges.korbit import _parse_candle

    raw = load_fixture("korbit", "candles_1d")["data"][0]
    assert raw["timestamp"] == 1787842800000
    candle = _parse_candle(raw)
    assert candle.timestamp == 1787842800000
    assert candle.timestamp % 86_400_000 == 15 * 3_600_000

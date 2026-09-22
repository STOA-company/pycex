"""F3 — fetch_candles_history pages to the last closed bar and backs off on RateLimitError.

Page cursors and the backoff schedule are literals. Changing the page size,
stopping on a short page, or sleeping a different Retry-After fails these tests.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pycex.base import BaseExchange
from pycex.exceptions import NetworkError, NotSupportedError, RateLimitError
from pycex.models import Candle

_TF = 60_000


def _bar(ts: int) -> Candle:
    return Candle(timestamp=ts, open=1, high=1, low=1, close=1, volume=1)


class _Hist(BaseExchange):
    name = "hist"
    candle_page_limit = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int | None, int | None, int]] = []

    def to_native(self, symbol: str) -> str:
        return symbol

    def from_native(self, native: str) -> str:
        return native

    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        self.calls.append((since, until, limit))
        start = -(-(since or 0) // _TF) * _TF
        return [_bar(start + i * _TF) for i in range(limit)]

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


class _EmptyAfter(_Hist):
    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        self.calls.append((since, until, limit))
        if since is not None and since >= _TF:
            return []
        return [_bar(0), _bar(_TF)]


class _Backward(_Hist):
    candle_paging = "backward"
    NOW = 10 * _TF

    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        self.calls.append((since, until, limit))
        end = self.NOW if until is None else until
        bars = [t for t in range(0, self.NOW + _TF, _TF) if t <= end]
        newest_first = sorted(bars, reverse=True)[:limit]
        return [_bar(t) for t in newest_first]


class _Rate(_Hist):
    def __init__(self, failures: int, retry_after: float | None) -> None:
        super().__init__()
        self._failures = failures
        self._retry_after = retry_after

    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        self.calls.append((since, until, limit))
        if len(self.calls) <= self._failures:
            err = RateLimitError("limited", exchange="hist")
            err.retry_after = self._retry_after
            raise err
        return [_bar(0)]


class _Down(_Hist):
    async def _fetch_candles_page(self, native, timeframe, *, since, until, limit):
        self.calls.append((since, until, limit))
        raise NetworkError("down")


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


async def test_history_forward_page_boundary_is_two_bars() -> None:
    ex = _Hist()
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 4 * _TF)]
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF, 3 * _TF, 4 * _TF]
    assert [call[2] for call in ex.calls] == [2, 2, 2]
    assert len(ex.calls) == 3


async def test_history_stops_when_the_page_is_empty() -> None:
    ex = _EmptyAfter()
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 10 * _TF)]
    assert [c.timestamp for c in out] == [0, _TF]
    assert len(ex.calls) == 2
    assert ex.calls[1][2] == 2


async def test_history_backward_page_cursors() -> None:
    ex = _Backward()
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 4 * _TF)]
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF, 3 * _TF, 4 * _TF]
    assert [call[2] for call in ex.calls] == [2, 2, 2]
    assert [call[1] for call in ex.calls] == [4 * _TF, 3 * _TF - 1, _TF - 1]
    assert all(call[0] is None for call in ex.calls)


async def test_history_until_absent_stops_at_the_last_closed_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 10 * _TF / 1000)
    ex = _Hist()
    ex.candle_page_limit = 20
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0)]
    assert out[-1].timestamp == 9 * _TF
    assert 10 * _TF not in [c.timestamp for c in out]
    assert ex.calls[0][2] == 20


async def test_history_explicit_until_keeps_a_bar_that_is_still_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 10 * _TF / 1000)
    ex = _Hist()
    ex.candle_page_limit = 20
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 10 * _TF)]
    assert 10 * _TF in [c.timestamp for c in out]


async def test_history_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _patch_sleep(monkeypatch)
    ex = _Rate(failures=1, retry_after=0.25)
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 0)]
    assert [c.timestamp for c in out] == [0]
    assert slept == [0.25]
    assert len(ex.calls) == 2


async def test_history_backoff_without_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _patch_sleep(monkeypatch)
    ex = _Rate(failures=2, retry_after=None)
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 0)]
    assert [c.timestamp for c in out] == [0]
    assert slept == [1.0, 2.0]


async def test_history_retries_five_times_then_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _patch_sleep(monkeypatch)
    ex = _Rate(failures=5, retry_after=None)
    out = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 0)]
    assert [c.timestamp for c in out] == [0]
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(ex.calls) == 6


async def test_history_sixth_rate_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _patch_sleep(monkeypatch)
    ex = _Rate(failures=6, retry_after=None)
    with pytest.raises(RateLimitError):
        _ = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 0)]
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(ex.calls) == 6


async def test_history_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _patch_sleep(monkeypatch)
    ex = _Down()
    with pytest.raises(NetworkError):
        _ = [c async for c in ex.fetch_candles_history("BTC/USDT", "1m", 0, 0)]
    assert slept == []
    assert len(ex.calls) == 1


async def test_history_rejects_an_unsupported_timeframe() -> None:
    ex = _Hist()
    with pytest.raises(NotSupportedError):
        _ = [c async for c in ex.fetch_candles_history("BTC/USDT", "3m", 0)]
    assert ex.calls == []


def test_history_sync_returns_the_same_bars() -> None:
    ex = _Hist()
    out = ex.fetch_candles_history_sync("BTC/USDT", "1m", 0, 4 * _TF)
    assert [c.timestamp for c in out] == [0, _TF, 2 * _TF, 3 * _TF, 4 * _TF]


async def test_history_sync_inside_loop_raises() -> None:
    with pytest.raises(RuntimeError):
        _Hist().fetch_candles_history_sync("BTC/USDT", "1m", 0, 0)

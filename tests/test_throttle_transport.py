"""X8 acceptance at the real adapter/HTTP boundary; no live orders or sockets."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from pycex.exceptions import RateLimitError
from pycex.exchanges.binance import Binance
from pycex.exchanges.bitget import Bitget
from pycex.exchanges.bithumb import Bithumb
from pycex.exchanges.korbit import Korbit
from pycex.exchanges.okx import OKX
from pycex.exchanges.upbit import Upbit
from pycex.ratelimit import ExchangeRateLimiter
from tests.test_exchange_rate_limiter import FakeClock

CASES = [
    (Upbit, "spot", 9, 1),
    (Binance, "spot", 80, 10),
    (Binance, "linear", 240, 10),
    (Bithumb, "spot", 8, 1),
    (Korbit, "spot", 24, 1),
    (Bitget, "linear", 5, 1),
    (OKX, "linear", 48, 2),
]


def order_response(name: str, number: int) -> Any:
    if name in {"upbit", "bithumb"}:
        return {"uuid": str(number), "market": "KRW-BTC", "side": "bid", "ord_type": "limit"}
    if name == "binance":
        return {"orderId": number, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT"}
    if name == "korbit":
        return {"success": True, "data": {"orderId": number}}
    if name == "bitget":
        return {"code": "00000", "data": {"orderId": str(number)}}
    return {"code": "0", "data": [{"ordId": str(number), "sCode": "0"}]}


@pytest.mark.parametrize("adapter,market_type,limit,period", CASES)
async def test_adapter_order_overflow_waits_fifo_with_zero_429(
    adapter: Any, market_type: str, limit: int, period: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ex = adapter(api_key="test", secret="test", market_type=market_type)
    clock = FakeClock()
    ex._rate_limiter = ExchangeRateLimiter(ex.name, market_type, clock=clock, sleep=clock.sleep)
    # Isolate the new layer: the legacy HTTP limiter must not hide a missing gate.
    monkeypatch.setattr(ex._http._limiter, "acquire", AsyncMock())
    accepted: list[float] = []
    rejected = 0
    signed_at: list[float] = []
    if isinstance(ex, Binance):
        original_sign = ex._signed_params

        def sign(params: Any = None) -> Any:
            signed_at.append(clock())
            return original_sign(params)

        monkeypatch.setattr(ex, "_signed_params", sign)

    async def server(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        assert request.method == "POST"
        if sum(t > clock() - period for t in accepted) >= limit:
            rejected += 1
            return httpx.Response(429, json={"msg": "too many requests"})
        accepted.append(clock())
        number = len(accepted)
        await asyncio.sleep(0)
        return httpx.Response(200, json=order_response(ex.name, number))

    ex._http.set_transport_factory(lambda: httpx.MockTransport(server))
    symbol = "BTC/KRW" if ex.name in {"upbit", "bithumb", "korbit"} else "BTC/USDT"
    if market_type == "linear":
        symbol += ":USDT"
    count = limit * 2 + 1
    try:
        orders = await asyncio.gather(*(ex.create_order(symbol, "buy", "limit", 1, 100) for _ in range(count)))
        assert [o.id for o in orders] == [str(i) for i in range(1, count + 1)]
        assert rejected == 0
        assert len(accepted) == count
        assert clock.now >= 2 * period
        assert clock.sleeps
        if isinstance(ex, Binance):
            assert signed_at == accepted, "signatures must be created after waiting, immediately before dispatch"
    finally:
        await ex.close()


@pytest.mark.parametrize("adapter,market_type,limit,period", CASES)
async def test_429_mapping_and_recovery_releases_admission(
    adapter: Any, market_type: str, limit: int, period: int
) -> None:
    ex = adapter(api_key="test", secret="test", market_type=market_type)
    calls = 0

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"msg": "too many requests"})
        return httpx.Response(200, json=order_response(ex.name, calls))

    ex._http.set_transport_factory(lambda: httpx.MockTransport(server))
    symbol = "BTC/KRW" if ex.name in {"upbit", "bithumb", "korbit"} else "BTC/USDT"
    if market_type == "linear":
        symbol += ":USDT"
    try:
        with pytest.raises(RateLimitError):
            await ex.create_order(symbol, "buy", "limit", 1, 100)
        order = await asyncio.wait_for(ex.create_order(symbol, "buy", "limit", 1, 100), 2)
        assert order.id == "2"
        assert calls == 2  # no implicit retry/replay of the rejected order
    finally:
        await ex.close()


async def test_bitget_1001_mapping_is_preserved() -> None:
    ex = Bitget(api_key="test", secret="test", market_type="linear")
    ex._http.set_transport_factory(
        lambda: httpx.MockTransport(lambda request: httpx.Response(200, json={"code": "1001", "msg": "frequent"}))
    )
    try:
        with pytest.raises(RateLimitError):
            await ex.create_order("BTC/USDT:USDT", "buy", "limit", 1, 100)
    finally:
        await ex.close()


@pytest.mark.parametrize("market_type,weight,budget", [("spot", 250, 4800), ("linear", 20, 1920)])
async def test_binance_depth_weight_exhaustion_waits_before_http(market_type: str, weight: int, budget: int) -> None:
    ex = Binance(market_type=market_type)
    clock = FakeClock()
    ex._rate_limiter = ExchangeRateLimiter("binance", market_type, clock=clock, sleep=clock.sleep)
    grants: list[float] = []
    rejected = 0

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        assert request.url.params["limit"] == "5000"
        if sum(t > clock() - 60 for t in grants) * weight + weight > budget:
            rejected += 1
            return httpx.Response(429, json={"msg": "weight exhausted"})
        grants.append(clock())
        return httpx.Response(200, json={"bids": [], "asks": [], "lastUpdateId": 1})

    ex._http.set_transport_factory(lambda: httpx.MockTransport(server))
    symbol = "BTC/USDT" + (":USDT" if market_type == "linear" else "")
    try:
        for _ in range(budget // weight + 1):
            await ex.fetch_order_book(symbol, limit=5000)
        assert rejected == 0
        assert clock.now >= 60
    finally:
        await ex.close()


async def test_legacy_http_gate_never_waits_after_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real legacy acquire stays nonblocking even when the clock hasn't advanced."""
    ex = Binance()
    monkeypatch.setattr("pycex.http.time.monotonic", lambda: ex._http._limiter._last)
    try:
        for _ in range(7000):
            await asyncio.wait_for(ex._http._limiter.acquire(), 0.1)
    finally:
        await ex.close()


async def test_binance_linear_all_open_orders_charges_forty_weight() -> None:
    clock = FakeClock()
    ex = Binance(api_key="test", secret="test", market_type="linear")
    ex._rate_limiter = ExchangeRateLimiter("binance", "linear", clock=clock, sleep=clock.sleep)
    async with ex._rate_limiter.request("query", weight=1881):
        pass
    ex._http.set_transport_factory(lambda: httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    try:
        assert await ex.fetch_open_orders() == []
        assert clock.now >= 60, "all-symbol openOrders costs 40, exceeding the remaining 39 weight"
    finally:
        await ex.close()

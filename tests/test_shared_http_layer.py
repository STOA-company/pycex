"""A2-3 — ``src/pycex/http.py`` 는 7개 거래소가 공유하는 계층이다.

A2-1·A2-2 는 OKX 를 고치려고 만졌지만 고쳐진 파일은 **전부의 것**이다.
그래서 여기서는 «파일에 있다»가 아니라 **실제로 돌려서** 확인한다 —
OKX 가 아닌 거래소가 sync 트윈을 두 번 돌고, 그 사이에 주입한 전송계층이
살아남고, 레이트 리밋 예산이 이어지는지.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pycex import OKX, Binance, Bitget, Bithumb, Bybit, Korbit, Upbit
from pycex.base import BaseExchange
from pycex.ratelimit import ExchangeRateLimiter
from tests.test_exchange_rate_limiter import FakeClock

ALL_EXCHANGES = [Binance, Bitget, Bithumb, Bybit, Korbit, OKX, Upbit]

_BINANCE_TICKER = {
    "symbol": "BTCUSDT",
    "lastPrice": "1.0",
    "bidPrice": "0.9",
    "askPrice": "1.1",
    "highPrice": "2.0",
    "lowPrice": "0.5",
    "volume": "10",
    "quoteVolume": "10",
    "closeTime": 1757462400000,
}


def _transport_name(ex: BaseExchange) -> str:
    client = ex._http._client_obj
    assert client is not None
    return type(client._transport).__name__


def test_binance_sync_twin_repeats_without_touching_the_network() -> None:
    """타 거래소 런타임 회귀 — 바이낸스 ``fetch_ticker_sync`` 2회, 목 도달 2회."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_BINANCE_TICKER)

    ex = Binance()
    ex._http.set_transport_factory(lambda: httpx.MockTransport(handler))

    assert ex.fetch_ticker_sync("BTC/USDT").last == 1.0
    assert _transport_name(ex) == "MockTransport"
    assert ex.fetch_ticker_sync("BTC/USDT").last == 1.0
    assert _transport_name(ex) == "MockTransport", "2회차가 실 전송계층으로 승격됐다"
    assert len(seen) == 2


def test_binance_rate_limit_budget_carries_across_sync_calls() -> None:
    """바이낸스에서도 예산은 인스턴스의 것이다 — 리미터가 갈리지 않는다."""
    ex = Binance()
    ex._http.set_transport_factory(lambda: httpx.MockTransport(lambda r: httpx.Response(200, json=_BINANCE_TICKER)))
    clock = FakeClock()
    limiter = ex._rate_limiter = ExchangeRateLimiter("binance", clock=clock, sleep=clock.sleep)
    ex.fetch_ticker_sync("BTC/USDT")
    after_one = limiter._buckets["total"].tokens
    ex.fetch_ticker_sync("BTC/USDT")
    assert ex._rate_limiter is limiter
    assert limiter._buckets["total"].tokens == after_one - 2


@pytest.mark.parametrize("cls", ALL_EXCHANGES, ids=lambda c: c.__name__)
def test_every_exchange_keeps_transport_and_budget_across_loops(cls: type[BaseExchange]) -> None:
    """공유 계층 계약을 7개 전부에 대해 «돌려서» 확인한다.

    ``*_sync`` 트윈과 같은 방식(호출마다 새 루프 + close)으로 두 번 태운다.
    """
    made: list[httpx.MockTransport] = []

    def factory() -> httpx.AsyncBaseTransport:
        t = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        made.append(t)
        return t

    ex = cls()
    ex._http.set_transport_factory(factory)
    limiter = ex._http._limiter
    # X8 moves admission before signing into the adapter. Every venue, including
    # Bybit, keeps that budget on the exchange instance across loops.
    throttle = getattr(ex, "_rate_limiter", None)
    if throttle is not None:
        clock = FakeClock()
        throttle = ExchangeRateLimiter(ex.name, clock=clock, sleep=clock.sleep)
        ex._rate_limiter = throttle  # type: ignore[attr-defined]

    def budget() -> float:
        if throttle is None:
            return limiter._tokens
        return throttle._buckets["total" if ex.name == "binance" else "query"].tokens

    def _one_loop() -> None:
        async def _run() -> None:
            if throttle is None:
                await ex._http.get("/ping")
            else:
                async with throttle.request("query"):
                    await ex._http.get("/ping")
            await ex._http.close()

        asyncio.run(_run())

    _one_loop()
    after_one = budget()
    assert _transport_name(ex) == "MockTransport"
    _one_loop()
    assert _transport_name(ex) == "MockTransport", f"{cls.__name__} 2회차가 실 네트워크로 나갔다"
    assert len(made) == 2, f"{cls.__name__} 재빌드가 팩토리를 부르지 않았다"
    assert ex._http._limiter is limiter, f"{cls.__name__} 리미터가 갈렸다"
    if throttle is not None:
        assert ex._rate_limiter is throttle  # type: ignore[attr-defined]
    assert budget() < after_one, f"{cls.__name__} 예산이 리필됐다"


# ── 우회 거부 ──


@pytest.mark.parametrize("cls", ALL_EXCHANGES, ids=lambda c: c.__name__)
def test_bypass_1_object_injection_rejected_on_every_exchange(cls: type[BaseExchange]) -> None:
    """우회 ① — 옛 객체 주입 경로는 7개 전부에서 막힌다(한 곳만 고치지 않았다)."""
    ex = cls()
    with pytest.raises(AttributeError):
        ex._http._client = httpx.AsyncClient(base_url="https://example.invalid")  # type: ignore[misc]
    ex.close_sync()


def test_bypass_2_none_factory_rejected_on_another_exchange() -> None:
    """우회 ② — «transport 없음» 팩토리는 OKX 밖에서도 거부된다."""
    ex = Binance()
    ex._http.set_transport_factory(lambda: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="transport_factory"):
        ex.fetch_ticker_sync("BTC/USDT")

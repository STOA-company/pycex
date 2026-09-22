"""Contracts for the exchange-specific pre-flight throttle (X8-1/X8-2)."""

from __future__ import annotations

import asyncio

import pytest

from pycex.constants import OKX_ORDER_RATE_LIMIT
from pycex.ratelimit import ExchangeRateLimiter, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def _request(limiter: ExchangeRateLimiter, kind: str, observed: list[float], clock: FakeClock) -> None:
    async with limiter.request(kind):
        observed.append(clock())


async def test_token_bucket_waits_after_budget_is_consumed() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2, 1, clock=clock, sleep=clock.sleep)

    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()

    assert clock.sleeps == [pytest.approx(0.5)]


async def test_token_bucket_weight_is_part_of_the_budget() -> None:
    clock = FakeClock()
    bucket = TokenBucket(10, 1, clock=clock, sleep=clock.sleep)

    await bucket.acquire(weight=10)
    await bucket.acquire(weight=1)

    assert clock.sleeps == [pytest.approx(0.1)]


async def test_upbit_order_and_query_budgets_are_separate() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    observed: list[float] = []

    for _ in range(24):
        await _request(limiter, "query", observed, clock)
    for _ in range(9):
        await _request(limiter, "order", observed, clock)

    assert clock.sleeps == []
    await _request(limiter, "order", observed, clock)
    assert clock.sleeps, "the tenth Upbit order must wait instead of producing a 429"


async def test_binance_spot_weight_and_order_budgets_are_separate() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("binance", "spot", clock=clock, sleep=clock.sleep)
    observed: list[float] = []

    # A weight charge must not consume the separate order-count allowance.
    async with limiter.request("query", weight=4800):
        observed.append(clock())
    for _ in range(80):
        async with limiter.request("order", weight=0):
            observed.append(clock())
    assert clock.sleeps == []

    async with limiter.request("order", weight=0):
        observed.append(clock())
    assert clock.sleeps, "the 81st Binance spot order must wait"


async def test_orders_are_fifo_when_they_wait() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    seen: list[str] = []

    for _ in range(9):
        async with limiter.request("order"):
            pass

    async def submit(name: str) -> None:
        async with limiter.request("order"):
            seen.append(name)

    first = asyncio.create_task(submit("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(submit("second"))
    await asyncio.gather(first, second)

    assert seen == ["first", "second"]


def test_budget_survives_sync_style_event_loop_rebuilds() -> None:
    """The limiter is owned by the exchange session, not one asyncio.run call."""
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)

    async def exhaust_budget() -> None:
        for _ in range(9):
            async with limiter.request("order"):
                pass

    async def one_more() -> None:
        async with limiter.request("order"):
            pass

    asyncio.run(exhaust_budget())
    asyncio.run(one_more())

    assert clock.sleeps, "새 event loop가 주문 버킷을 가득 찬 상태로 재생성했다"


async def test_okx_queries_are_not_single_flight() -> None:
    limiter = ExchangeRateLimiter("okx")
    first_entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with limiter.request("query"):
            first_entered.set()
            await release.wait()

    async def second() -> None:
        async with limiter.request("query"):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert second_entered.is_set()
    release.set()
    await asyncio.gather(first_task, second_task)


async def test_preflight_sequence_waits_and_never_reaches_a_429_server() -> None:
    """This is the X8-2 regression: removing the bucket makes the server raise."""
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    accepted: list[float] = []
    rejected = 0

    async def server_request() -> None:
        nonlocal rejected
        async with limiter.request("order"):
            # Published Upbit order-create cap is 12/s. The client stays at 9/s.
            if sum(t > clock() - 1 for t in accepted) >= 12:
                rejected += 1
                raise RuntimeError("429")
            accepted.append(clock())

    await asyncio.gather(*(server_request() for _ in range(17)))

    assert len(accepted) == 17
    assert rejected == 0
    assert clock.sleeps, "an over-limit sequence must wait before sending"


@pytest.mark.parametrize("group", ["ticker", "orderbook", "candle", "trade", "market"])
async def test_upbit_quotation_groups_enforce_eight_each(group: str) -> None:
    """Each quotation group is 80% of the published 10/s IP cap, separate from default."""
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    for _ in range(8):
        async with limiter.request("query", group=group):
            pass
    other = "market" if group != "market" else "ticker"
    async with limiter.request("query", group=other):
        assert clock.now == 0
    async with limiter.request("query", group=group):
        assert clock.now >= 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0, -1])
def test_token_bucket_rejects_invalid_configuration(value: float) -> None:
    with pytest.raises(ValueError):
        TokenBucket(value, 1)
    with pytest.raises(ValueError):
        TokenBucket(1, value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -1])
async def test_exchange_rejects_invalid_weight_without_poisoning_budget(value: float) -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError):
        async with limiter.request("order", weight=value):
            pass
    async with limiter.request("order"):
        assert clock.now == 0


async def test_cancelled_waiter_does_not_block_following_order() -> None:
    sleeping = asyncio.Event()
    release_sleep = asyncio.Event()
    clock = FakeClock()

    async def controlled_sleep(delay: float) -> None:
        sleeping.set()
        await release_sleep.wait()
        await clock.sleep(delay)

    limiter = ExchangeRateLimiter("okx", clock=clock, sleep=controlled_sleep)
    for _ in range(OKX_ORDER_RATE_LIMIT[0]):
        async with limiter.request("order"):
            pass

    async def submit() -> None:
        async with limiter.request("order"):
            pass

    cancelled = asyncio.create_task(submit())
    await sleeping.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release_sleep.set()
    await asyncio.wait_for(submit(), 1)
    assert clock.now >= 1


async def test_binance_order_also_waits_for_shared_weight() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("binance", clock=clock, sleep=clock.sleep)
    async with limiter.request("query", weight=4800):
        pass
    async with limiter.request("order", weight=1):
        assert clock.now >= 60


async def test_first_order_can_run_while_same_loop_query_is_inflight() -> None:
    limiter = ExchangeRateLimiter("upbit")
    async with limiter.request("query"):
        async with limiter.request("order"):
            pass

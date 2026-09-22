"""Exchange-specific pre-flight request throttles.

The limiter is intentionally independent of :mod:`pycex.http`: adapters admit
a request immediately before signing and dispatching it.  That keeps a queued
private request from acquiring a stale timestamp or signature.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from pycex.constants import (
    BINANCE_LINEAR_ORDER_MINUTE_RATE_LIMIT,
    BINANCE_LINEAR_ORDER_RATE_LIMIT,
    BINANCE_LINEAR_WEIGHT_RATE_LIMIT,
    BINANCE_SPOT_ORDER_RATE_LIMIT,
    BINANCE_SPOT_WEIGHT_RATE_LIMIT,
    BITGET_ORDER_RATE_LIMIT,
    BITGET_PUBLIC_RATE_LIMIT,
    BITHUMB_ORDER_RATE_LIMIT,
    BITHUMB_PUBLIC_RATE_LIMIT,
    BYBIT_IP_RATE_LIMIT,
    BYBIT_LINEAR_ORDER_RATE_LIMIT,
    BYBIT_PRIVATE_QUERY_RATE_LIMIT,
    BYBIT_SPOT_ORDER_RATE_LIMIT,
    DEFAULT_MAX_INFLIGHT,
    KORBIT_ORDER_RATE_LIMIT,
    KORBIT_PUBLIC_RATE_LIMIT,
    OKX_ORDER_RATE_LIMIT,
    OKX_QUERY_RATE_LIMIT,
    UPBIT_ORDER_RATE_LIMIT,
    UPBIT_PUBLIC_RATE_LIMIT,
    UPBIT_QUERY_RATE_LIMIT,
)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """A weighted token bucket whose balance survives sequential event loops."""

    def __init__(
        self,
        limit: float,
        period: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not math.isfinite(limit) or not math.isfinite(period) or limit <= 0 or period <= 0:
            raise ValueError("limit and period must be finite and positive")
        self.limit = float(limit)
        self.period = float(period)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.limit
        self._last = clock()
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._active = 0

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            if self._active:
                raise RuntimeError("TokenBucket cannot be used concurrently from different event loops")
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.limit, self._tokens + elapsed * self.limit / self.period)
        self._last = now

    async def acquire(self, weight: float = 1) -> None:
        """Wait until ``weight`` tokens can be charged.

        The lock remains held while waiting.  This supplies FIFO admission and
        makes cancellation harmless: a cancelled waiter has not been charged.
        """
        weight = float(weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("weight must be finite and positive")
        if weight > self.limit:
            raise ValueError("weight exceeds bucket capacity")
        lock = self._lock_for_loop()
        async with lock:
            self._active += 1
            try:
                while True:
                    self._refill()
                    if self._tokens >= weight:
                        self._tokens -= weight
                        return
                    await self._sleep((weight - self._tokens) * self.period / self.limit)
            finally:
                self._active -= 1


@dataclass
class _Window:
    limit: float
    period: float
    tokens: float
    last: float
    grants: deque[tuple[float, float]] = field(default_factory=deque)

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.limit, self.tokens + elapsed * self.limit / self.period)
        self.last = now
        cutoff = now - self.period
        while self.grants and self.grants[0][0] <= cutoff:
            self.grants.popleft()

    def wait_for(self, weight: float, now: float) -> float:
        token_wait = max(0.0, (weight - self.tokens) * self.period / self.limit)
        used = sum(grant for _, grant in self.grants)
        if used + weight <= self.limit:
            return token_wait
        released = used
        window_wait = 0.0
        for timestamp, grant in self.grants:
            released -= grant
            if released + weight <= self.limit:
                window_wait = max(0.0, timestamp + self.period - now)
                break
        return max(token_wait, window_wait)

    def charge(self, weight: float, now: float) -> None:
        self.tokens -= weight
        self.grants.append((now, weight))


def _window(limit_period: tuple[int, float], now: float) -> _Window:
    limit, period = limit_period
    return _Window(float(limit), period, float(limit), now)


class ExchangeRateLimiter:
    """Atomically admits a request against every applicable venue budget.

    A token bucket controls average flow and a rolling window prevents an
    initial burst plus refills from crossing the venue's fixed-window 429 gate.
    The context remains open through HTTP completion when a venue needs an
    in-flight concurrency bound.
    """

    def __init__(
        self,
        exchange: str,
        market_type: str = "spot",
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.exchange = exchange.lower()
        self.market_type = market_type.lower()
        self._clock = clock
        self._sleep = sleep
        now = clock()
        self._buckets: dict[str, _Window] = {}
        self._public_buckets: dict[str, _Window] = {}
        self._concurrency: int | None = None
        self._configure(now)
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        # Orders are serialized through dispatch and completion.  It protects
        # the server-visible FIFO property even at venues whose *queries* may
        # run concurrently; conservative venues instead use the shared gate.
        self._order_semaphore: asyncio.Semaphore | None = None
        self._order_semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._admitting = 0
        self._inflight = 0
        self._inflight_loop: asyncio.AbstractEventLoop | None = None

    def _configure(self, now: float) -> None:
        def add(name: str, limit_period: tuple[int, float]) -> None:
            self._buckets[name] = _window(limit_period, now)

        def add_split(query: tuple[int, float], order: tuple[int, float]) -> None:
            if query[1] != order[1]:
                raise ValueError("split rate limits must share a period")
            add("query", query)
            add("order", order)
            # The shared window must not be tighter than either class.
            add("total", (max(query[0], order[0]), query[1]))
            self._concurrency = DEFAULT_MAX_INFLIGHT

        if self.exchange == "upbit":
            add("query", UPBIT_QUERY_RATE_LIMIT)
            add("order", UPBIT_ORDER_RATE_LIMIT)
        elif self.exchange == "binance":
            if self.market_type == "linear":
                add("total", BINANCE_LINEAR_WEIGHT_RATE_LIMIT)
                add("order", BINANCE_LINEAR_ORDER_RATE_LIMIT)
                add("order_minute", BINANCE_LINEAR_ORDER_MINUTE_RATE_LIMIT)
            elif self.market_type == "spot":
                add("total", BINANCE_SPOT_WEIGHT_RATE_LIMIT)
                add("order", BINANCE_SPOT_ORDER_RATE_LIMIT)
            else:
                raise ValueError(f"unsupported Binance market_type: {self.market_type!r}")
        elif self.exchange == "bybit":
            add("query", BYBIT_IP_RATE_LIMIT)
            add("private", BYBIT_PRIVATE_QUERY_RATE_LIMIT)
            if self.market_type == "linear":
                add("order", BYBIT_LINEAR_ORDER_RATE_LIMIT)
            elif self.market_type == "spot":
                add("order", BYBIT_SPOT_ORDER_RATE_LIMIT)
            else:
                raise ValueError(f"unsupported Bybit market_type: {self.market_type!r}")
            self._concurrency = DEFAULT_MAX_INFLIGHT
        elif self.exchange == "okx":
            add_split(OKX_QUERY_RATE_LIMIT, OKX_ORDER_RATE_LIMIT)
        elif self.exchange == "bitget":
            add_split(BITGET_PUBLIC_RATE_LIMIT, BITGET_ORDER_RATE_LIMIT)
        elif self.exchange == "bithumb":
            add_split(BITHUMB_PUBLIC_RATE_LIMIT, BITHUMB_ORDER_RATE_LIMIT)
        elif self.exchange == "korbit":
            add_split(KORBIT_PUBLIC_RATE_LIMIT, KORBIT_ORDER_RATE_LIMIT)
        else:
            raise ValueError(f"unsupported exchange for rate limiting: {self.exchange!r}")

    def _loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._inflight and self._inflight_loop is not loop:
            raise RuntimeError("ExchangeRateLimiter cannot be used concurrently from different event loops")
        if self._lock_loop is not loop:
            if self._admitting:
                raise RuntimeError("ExchangeRateLimiter cannot be used concurrently from different event loops")
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _loop_semaphore(self) -> asyncio.Semaphore | None:
        if self._concurrency is None:
            return None
        loop = asyncio.get_running_loop()
        if self._semaphore_loop is not loop:
            if self._inflight or self._admitting:
                raise RuntimeError("ExchangeRateLimiter cannot be used concurrently from different event loops")
            self._semaphore = asyncio.Semaphore(self._concurrency)
            self._semaphore_loop = loop
        return self._semaphore

    def _loop_order_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._order_semaphore_loop is not loop:
            if (self._inflight and self._inflight_loop is not loop) or (
                self._admitting and self._lock_loop is not loop
            ):
                raise RuntimeError("ExchangeRateLimiter cannot be used concurrently from different event loops")
            self._order_semaphore = asyncio.Semaphore(1)
            self._order_semaphore_loop = loop
        assert self._order_semaphore is not None
        return self._order_semaphore

    def _selected(self, kind: str, group: str | None, weight: float) -> list[tuple[_Window, float]]:
        if kind not in {"query", "order"}:
            raise ValueError("kind must be 'query' or 'order'")
        names: list[str]
        if self.exchange == "binance":
            # Binance's ORDERS quota counts an order once, independently from
            # its REQUEST_WEIGHT cost.  ``weight=0`` is therefore useful for
            # an order endpoint whose shared request-weight charge is known to
            # be zero, while it can never bypass the order-count quota.
            selected = [(self._buckets["total"], weight)]
            if kind == "order":
                selected.append((self._buckets["order"], 1.0))
                minute = self._buckets.get("order_minute")
                if minute is not None:
                    selected.append((minute, 1.0))
            return [(bucket, charge) for bucket, charge in selected if charge]
        if self.exchange == "bybit":
            # Public calls spend the IP window. Private GETs also spend the
            # tighter UID window. Orders spend both the IP window and the
            # order window.
            selected = [(self._buckets["query"], weight)]
            if group == "private":
                selected.append((self._buckets["private"], weight))
            if kind == "order":
                selected.append((self._buckets["order"], weight))
            return [(bucket, charge) for bucket, charge in selected if charge]
        else:
            names = [kind]
            if self.exchange == "upbit" and group is not None:
                public = self._public_buckets.get(group)
                if public is None:
                    public = _window(UPBIT_PUBLIC_RATE_LIMIT, self._clock())
                    self._public_buckets[group] = public
                return [(self._buckets[name], weight) for name in names] + [(public, weight)]
            if self.exchange in {"bithumb", "korbit", "bitget", "okx"}:
                names.append("total")
        return [(self._buckets[name], weight) for name in names]

    @asynccontextmanager
    async def request(self, kind: str = "query", *, weight: float = 1, group: str | None = None) -> AsyncIterator[None]:
        """Reserve budget before signing/dispatch, and release concurrency on exit."""
        weight = float(weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("weight must be finite and non-negative")
        if self.exchange != "binance" and weight == 0:
            raise ValueError("weight must be positive")
        selected = self._selected(kind, group, weight)
        if not selected:
            raise ValueError("query requests must have a positive weight")
        if any(charge > bucket.limit for bucket, charge in selected):
            raise ValueError("weight exceeds bucket capacity")
        semaphore = (
            self._loop_semaphore()
            if self._concurrency is not None
            else (self._loop_order_semaphore() if kind == "order" else None)
        )
        lock = self._loop_lock()
        semaphore_acquired = False
        async with lock:
            self._admitting += 1
            try:
                if semaphore is not None:
                    await semaphore.acquire()
                    semaphore_acquired = True
                    self._inflight += 1
                    self._inflight_loop = asyncio.get_running_loop()
                while True:
                    now = self._clock()
                    for bucket, _ in selected:
                        bucket.refill(now)
                    wait = max(bucket.wait_for(charge, now) for bucket, charge in selected)
                    if wait <= 0:
                        for bucket, charge in selected:
                            bucket.charge(charge, now)
                        break
                    await self._sleep(wait)
                if semaphore is None:
                    self._inflight += 1
                    self._inflight_loop = asyncio.get_running_loop()
            except BaseException:
                if semaphore_acquired:
                    assert semaphore is not None
                    semaphore.release()
                    self._inflight -= 1
                    if not self._inflight:
                        self._inflight_loop = None
                raise
            finally:
                self._admitting -= 1
        try:
            yield
        finally:
            if semaphore is not None:
                semaphore.release()
            self._inflight -= 1
            if not self._inflight:
                self._inflight_loop = None

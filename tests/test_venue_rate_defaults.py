"""Defaults match the published-cap table (80%, floored) and a fake clock cannot outrun it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from pycex.exchanges.okx import OKX
from pycex.ratelimit import ExchangeRateLimiter
from tests.test_exchange_rate_limiter import FakeClock

ROOT = Path(__file__).resolve().parents[1]

# Literal rows. Same numbers as $WORK/LIMITS.md and the README «Rate limits» table.
# concurrency "" means the limiter does not add an in-flight cap.
LIMIT_ROWS: tuple[tuple[str, str, str, int, float, int | None], ...] = (
    ("binance", "spot", "total", 4800, 60.0, None),
    ("binance", "spot", "order", 80, 10.0, None),
    ("binance", "linear", "total", 1920, 60.0, None),
    ("binance", "linear", "order", 240, 10.0, None),
    ("binance", "linear", "order_minute", 960, 60.0, None),
    ("bybit", "spot", "query", 480, 5.0, 4),
    ("bybit", "spot", "order", 16, 1.0, 4),
    ("bybit", "linear", "query", 480, 5.0, 4),
    ("bybit", "linear", "order", 8, 1.0, 4),
    ("bybit", "spot", "private", 40, 1.0, 4),
    ("bybit", "linear", "private", 40, 1.0, 4),
    ("okx", "spot", "query", 16, 2.0, 4),
    ("okx", "spot", "order", 5, 1.0, 4),
    ("bitget", "spot", "query", 16, 1.0, 4),
    ("bitget", "spot", "order", 5, 1.0, 4),
    ("upbit", "spot", "query", 24, 1.0, None),
    ("upbit", "spot", "order", 9, 1.0, None),
    ("bithumb", "spot", "query", 120, 1.0, 4),
    ("bithumb", "spot", "order", 8, 1.0, 4),
    ("korbit", "spot", "query", 40, 1.0, 4),
    ("korbit", "spot", "order", 24, 1.0, 4),
)


def _readme_rows() -> list[tuple[str, str, str, int, float, int | None]]:
    text = (ROOT / "README.md").read_text()
    assert "## Rate limits" in text
    start = text.index("## Rate limits")
    section = text[start:].split("\n## ", 1)[0]
    rows: list[tuple[str, str, str, int, float, int | None]] = []
    for line in section.splitlines():
        if not line.startswith("| ") or line.startswith("| exchange") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or not cells[3].isdigit():
            continue
        concurrency = int(cells[5]) if cells[5] else None
        rows.append((cells[0], cells[1], cells[2], int(cells[3]), float(cells[4]), concurrency))
    return rows


def test_readme_rate_limit_table_matches_literals() -> None:
    assert _readme_rows() == list(LIMIT_ROWS)


@pytest.mark.parametrize("exchange,market,bucket,limit,period,concurrency", LIMIT_ROWS)
def test_limiter_default_matches_literal(
    exchange: str, market: str, bucket: str, limit: int, period: float, concurrency: int | None
) -> None:
    limiter = ExchangeRateLimiter(exchange, market)
    window = limiter._buckets[bucket]
    assert window.limit == limit
    assert window.period == period
    assert limiter._concurrency == concurrency


def test_upbit_candle_group_is_eight_per_second() -> None:
    """80% of the published quotation candle group (10/s)."""
    from pycex.constants import UPBIT_PUBLIC_RATE_LIMIT

    assert UPBIT_PUBLIC_RATE_LIMIT == (8, 1.0)


def test_okx_recent_candle_weight_is_half() -> None:
    """Weight 0.5 on the 16/2s bucket is 32/2s, 80% of the 40/2s recent-candle cap."""
    from pycex.constants import OKX_HISTORY_CANDLE_WEIGHT, OKX_RECENT_CANDLE_WEIGHT

    assert OKX_RECENT_CANDLE_WEIGHT == 0.5
    assert OKX_HISTORY_CANDLE_WEIGHT == 1.0


@pytest.mark.parametrize(
    "exchange,market,kind,weight,limit,period",
    [
        ("binance", "spot", "query", 1, 4800, 60.0),
        ("binance", "spot", "order", 0, 80, 10.0),
        ("okx", "spot", "query", 1, 16, 2.0),
        ("okx", "spot", "order", 1, 5, 1.0),
        ("bitget", "spot", "query", 1, 16, 1.0),
        ("bithumb", "spot", "query", 1, 120, 1.0),
        ("korbit", "spot", "query", 1, 40, 1.0),
        ("bybit", "spot", "query", 1, 480, 5.0),
        ("upbit", "spot", "query", 1, 24, 1.0),
        ("upbit", "spot", "order", 1, 9, 1.0),
    ],
)
async def test_bucket_fills_then_waits_inside_its_window(
    exchange: str, market: str, kind: str, weight: float, limit: int, period: float
) -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter(exchange, market, clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(limit):
        async with limiter.request(kind, weight=weight):
            stamps.append(clock.now)
    assert clock.now == 0
    async with limiter.request(kind, weight=weight):
        stamps.append(clock.now)
    assert clock.now > 0
    for stamp in stamps:
        in_window = [item for item in stamps if stamp - period < item <= stamp]
        assert len(in_window) <= limit


async def test_binance_linear_orders_honor_the_minute_window() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("binance", "linear", clock=clock, sleep=clock.sleep)
    for _ in range(961):
        async with limiter.request("order", weight=0):
            pass
    assert clock.now >= 60


async def test_upbit_candle_group_ninth_request_waits() -> None:
    clock = FakeClock()
    limiter = ExchangeRateLimiter("upbit", clock=clock, sleep=clock.sleep)
    for _ in range(8):
        async with limiter.request("query", group="candle"):
            pass
    assert clock.now == 0
    async with limiter.request("query", group="candle"):
        pass
    assert clock.now >= 1


@pytest.mark.parametrize("exchange", ["okx", "bitget", "bithumb", "korbit", "bybit"])
async def test_confirmed_venues_allow_four_inflight_and_block_a_fifth(exchange: str) -> None:
    limiter = ExchangeRateLimiter(exchange)
    entered = 0
    release = asyncio.Event()

    async def hold() -> None:
        nonlocal entered
        async with limiter.request("query"):
            entered += 1
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(4)]
    for _ in range(20):
        if entered == 4:
            break
        await asyncio.sleep(0)
    assert entered == 4
    fifth_entered = asyncio.Event()

    async def fifth() -> None:
        async with limiter.request("query"):
            fifth_entered.set()

    extra = asyncio.create_task(fifth())
    await asyncio.sleep(0)
    assert not fifth_entered.is_set()
    release.set()
    await asyncio.gather(*tasks, extra)


_OKX_BAR = ["1700000000000", "1", "2", "0.5", "1.5", "10"]


async def test_okx_recent_candle_charges_half_a_token() -> None:
    clock = FakeClock()
    ex = OKX()
    ex._rate_limiter = ExchangeRateLimiter("okx", clock=clock, sleep=clock.sleep)
    ex._http.set_transport_factory(
        lambda: httpx.MockTransport(lambda request: httpx.Response(200, json={"code": "0", "data": [_OKX_BAR]}))
    )
    try:
        before = ex._rate_limiter._buckets["query"].tokens
        await ex._fetch_candles_page("BTC-USDT", "1m", since=None, until=None, limit=100)
        assert ex._rate_limiter._buckets["query"].tokens == before - 0.5
    finally:
        await ex.close()


async def test_okx_history_candle_charges_a_full_token_after_an_empty_recent_page() -> None:
    clock = FakeClock()
    ex = OKX()
    ex._rate_limiter = ExchangeRateLimiter("okx", clock=clock, sleep=clock.sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"code": "0", "data": []})
        return httpx.Response(200, json={"code": "0", "data": [_OKX_BAR]})

    ex._http.set_transport_factory(lambda: httpx.MockTransport(handler))
    try:
        before = ex._rate_limiter._buckets["query"].tokens
        await ex._fetch_candles_page("BTC-USDT", "1m", since=1, until=None, limit=100)
        assert ex._rate_limiter._buckets["query"].tokens == before - 1.5
    finally:
        await ex.close()

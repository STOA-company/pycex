"""Bithumb KRW rules from the official 원화 마켓 거래 정책 안내 (support.bithumb.com/hc/ko/articles/51036972377241).

Recorded market-list fixture + synthetic transport only; no private endpoint is called.
"""

from decimal import Decimal

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.bithumb import Bithumb
from pycex.models.market import tick_for_price, tick_ladder
from tests.conftest import load_fixture

POLICY_URL = "https://support.bithumb.com/hc/ko/articles/51036972377241"


async def test_krw_rules_on_recorded_markets_and_raw_preserved(httpx_mock: HTTPXMock) -> None:
    rows = load_fixture("bithumb", "markets") + [{"market": "BTC-ETH"}]
    httpx_mock.add_response(json=rows)
    async with Bithumb() as exchange:
        markets = await exchange.fetch_markets()
        for market, row in zip(markets, rows):
            assert market.raw == row
            assert exchange._markets[market.native] is market
            assert market.price_tick is None  # scalar stays unset; consumers read the ladder
            if market.quote != "KRW":
                assert market.amount_step is None and market.min_notional is None
                assert market.public_rules == {}
                assert tick_ladder(market) is None
                continue
            rules = market.public_rules
            assert market.min_notional == 5000.0
            assert market.amount_step == 0.00000001
            assert rules["min_notional"] == "5000" and rules["notional_unit"] == "KRW"
            assert rules["amount_step"] == "0.00000001"
            assert rules["amount_unit"] == market.base
            assert rules["amount_step_scope"] == "order_quantity"
            # Not stated by the policy page: left None rather than guessed.
            assert rules["amount_rounding"] is None and rules["min_quantity"] is None
            assert rules["amount_step_source"] == rules["min_notional_source"] == POLICY_URL
            assert rules["price_tick_ladder_source"] == POLICY_URL
            assert rules["verified_on"] == rules["price_tick_ladder_verified_on"] == "2026-09-26"
            assert len(rules["price_tick_ladder"]) == 11
            assert market.model_validate_json(market.model_dump_json()).public_rules == rules
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "api.bithumb.com"
    assert requests[0].url.path == "/v1/market/all"
    assert "authorization" not in requests[0].headers


# (price, expected tick): every official floor ("N원 이상") and the price just below it.
LADDER_CASES = [
    ("0.0001", "0.0001"),
    ("0.9999", "0.0001"),
    ("1", "0.001"),
    ("9.999", "0.001"),
    ("10", "0.01"),
    ("99.99", "0.01"),
    ("100", "1"),
    ("999", "1"),
    ("1000", "1"),
    ("4999", "1"),
    ("5000", "5"),
    ("9995", "5"),
    ("10000", "10"),
    ("49990", "10"),
    ("50000", "50"),
    ("99950", "50"),
    ("100000", "100"),
    ("499900", "100"),
    ("500000", "500"),
    ("999500", "500"),
    ("1000000", "1000"),
    ("150000000", "1000"),
]


@pytest.mark.parametrize("native", ["KRW-BTC", "KRW-ETH", "KRW-ETC"])
@pytest.mark.parametrize(("price", "tick"), LADDER_CASES)
async def test_krw_ladder_boundaries(httpx_mock: HTTPXMock, native: str, price: str, tick: str) -> None:
    httpx_mock.add_response(json=[{"market": native}])
    async with Bithumb() as exchange:
        market = (await exchange.fetch_markets())[0]
    assert tick_for_price(market, price) == Decimal(tick)


async def test_ladder_first_floor_and_invalid_price(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}])
    async with Bithumb() as exchange:
        market = (await exchange.fetch_markets())[0]
    assert tick_for_price(market, "0.00000001") == Decimal("0.0001")  # "1원 미만" is the lowest tier
    for bad in ("0", "-1"):
        with pytest.raises(ValueError):
            tick_for_price(market, bad)


async def test_ladder_per_market_copy_and_min_notional_boundary(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}, {"market": "KRW-ETH"}])
    async with Bithumb() as exchange:
        first, second = await exchange.fetch_markets()
    assert tick_ladder(first) == tick_ladder(second)
    first.public_rules["price_tick_ladder"].pop()
    assert len(second.public_rules["price_tick_ladder"]) == 11  # mutating one market must not leak into another
    minimum = Decimal(second.public_rules["min_notional"])
    assert Decimal("4999.99") < minimum <= Decimal("5000")
    # Official 최소 주문수량 단위: 1만원어치 at 30,000,000 -> 0.00033333.. base units, quantized to 1e-8.
    step = Decimal(second.public_rules["amount_step"])
    assert (Decimal("10000") / Decimal("30000000")).quantize(step, rounding="ROUND_DOWN") == Decimal("0.00033333")

"""Document-derived KRW rules for Korbit: price_tick_ladder from GET /v2/tickSizePolicy (public)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.korbit import Korbit
from pycex.models.market import Market, tick_for_price, tick_ladder
from tests.conftest import load_fixture

POLICY = load_fixture("korbit", "tick_size_policy_xrp_krw")
PAIR = {
    "symbol": "xrp_krw",
    "status": "launched",
    "baseCurrency": "xrp",
    "quoteCurrency": "krw",
    "minOrderValue": "5000",
    "maxOrderValue": "1000000000",
}


def _pairs(*rows: dict) -> dict:
    return {"success": True, "data": list(rows)}


async def _market(httpx_mock: HTTPXMock, policy_response: dict | None = None, *, status_code: int = 200) -> Market:
    httpx_mock.add_response(url="https://api.korbit.co.kr/v2/currencyPairs", json=_pairs(PAIR))
    httpx_mock.add_response(
        url="https://api.korbit.co.kr/v2/tickSizePolicy?symbol=xrp_krw",
        json=policy_response if policy_response is not None else POLICY,
        status_code=status_code,
    )
    async with Korbit() as ex:
        return (await ex.fetch_markets())[0]


async def test_rules_same_keys_as_upbit_and_public_only(httpx_mock: HTTPXMock) -> None:
    market = await _market(httpx_mock)
    rules = market.public_rules
    assert market.price_tick is None and market.amount_step is None
    assert Decimal(str(market.min_notional)) == Decimal("5000")
    assert rules["amount_step"] is None  # Korbit publishes no quantity step
    assert rules["min_notional"] == "5000" and rules["max_notional"] == "1000000000"
    assert rules["min_quantity"] is None
    assert rules["amount_unit"] == "XRP" and rules["notional_unit"] == "KRW"
    assert rules["verified_on"] == "2026-09-26"
    assert rules["price_tick_ladder_source"].startswith("https://docs.digitalx.miraeasset.com/")
    assert rules["price_tick_ladder_verified_on"] == "2026-09-26"
    assert market.model_validate_json(market.model_dump_json()).public_rules == rules
    for req in httpx_mock.get_requests():
        assert req.method == "GET" and req.url.host == "api.korbit.co.kr"
        assert "x-kapi-key" not in req.headers and "signature" not in req.url.params


async def test_ladder_is_the_api_response_verbatim(httpx_mock: HTTPXMock) -> None:
    market = await _market(httpx_mock)
    expected = [{"min_price": t["priceGte"], "tick": t["tickSize"]} for t in POLICY["data"][0]["tickSizePolicy"]]
    assert market.public_rules["price_tick_ladder"] == expected
    assert len(expected) == 11


# (price, expected tick): every official floor, and the price just below each floor (docs example: 150 -> 0.1).
LADDER_CASES = [
    ("0.00001", "0.0001"),
    ("0.9999", "0.0001"),
    ("1", "0.001"),
    ("9.999", "0.001"),
    ("10", "0.01"),
    ("99.99", "0.01"),
    ("100", "0.1"),
    ("150", "0.1"),
    ("999.9", "0.1"),
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


@pytest.mark.parametrize(("price", "tick"), LADDER_CASES)
async def test_ladder_boundaries(httpx_mock: HTTPXMock, price: str, tick: str) -> None:
    market = await _market(httpx_mock)
    assert tick_for_price(market, price) == Decimal(tick)


async def test_ladder_reads_through_tick_ladder_helper(httpx_mock: HTTPXMock) -> None:
    ladder = tick_ladder(await _market(httpx_mock))
    assert ladder is not None and ladder[0] == (Decimal("0"), Decimal("0.0001"))
    assert ladder[-1] == (Decimal("1000000"), Decimal("1000"))


async def test_only_launched_krw_markets_get_rules_and_calls(httpx_mock: HTTPXMock) -> None:
    rows = [
        PAIR,
        {**PAIR, "symbol": "eth_btc", "baseCurrency": "eth", "quoteCurrency": "btc"},
        {**PAIR, "symbol": "kda_krw", "baseCurrency": "kda", "status": "stopped"},
    ]
    httpx_mock.add_response(url="https://api.korbit.co.kr/v2/currencyPairs", json=_pairs(*rows))
    httpx_mock.add_response(url="https://api.korbit.co.kr/v2/tickSizePolicy?symbol=xrp_krw", json=POLICY)
    async with Korbit() as ex:
        markets = await ex.fetch_markets()
    by_native = {m.native: m for m in markets}
    assert "price_tick_ladder" in by_native["xrp_krw"].public_rules
    assert by_native["eth_btc"].public_rules == {} and by_native["kda_krw"].public_rules == {}
    assert len(httpx_mock.get_requests()) == 2  # currencyPairs + one policy call


async def test_policy_error_leaves_market_without_ladder(httpx_mock: HTTPXMock) -> None:
    market = await _market(
        httpx_mock, {"success": False, "error": {"code": 500, "message": "INTERNAL"}}, status_code=500
    )
    assert "price_tick_ladder" not in market.public_rules
    assert market.public_rules["min_notional"] == "5000"
    assert tick_ladder(market) is None


@pytest.mark.parametrize(
    "bad_tiers",
    [
        [{"priceGte": "0"}],  # missing tickSize
        [{"priceGte": "0", "tickSize": "0.1"}, {"priceGte": "0", "tickSize": "0.2"}],  # duplicate floor
        [{"priceGte": "0", "tickSize": "0"}],  # zero tick
    ],
)
async def test_malformed_policy_is_dropped(httpx_mock: HTTPXMock, bad_tiers: list[dict]) -> None:
    body = {"success": True, "data": [{"symbol": "xrp_krw", "tickSizePolicy": bad_tiers, "orderbookLevels": []}]}
    market = await _market(httpx_mock, body)
    assert "price_tick_ladder" not in market.public_rules
    assert market.public_rules["min_notional"] == "5000"


async def test_policy_for_other_symbol_is_ignored(httpx_mock: HTTPXMock) -> None:
    other = {"success": True, "data": [{**POLICY["data"][0], "symbol": "btc_krw"}]}
    market = await _market(httpx_mock, other)
    assert "price_tick_ladder" not in market.public_rules


async def test_empty_markets(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_pairs())
    async with Korbit() as ex:
        assert await ex.fetch_markets() == []
        assert ex._markets == {}

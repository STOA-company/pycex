"""Document-derived KRW rules: units, scope and public-only acquisition."""

from decimal import ROUND_DOWN, Decimal

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.bithumb import Bithumb
from pycex.exchanges.upbit import Upbit
from pycex.models.market import tick_for_price, tick_ladder
from tests.conftest import load_fixture


async def test_rules_cover_all_krw_and_preserve_raw(httpx_mock: HTTPXMock) -> None:
    rows = load_fixture("upbit", "markets") + [
        {"market": native} for native in ("KRW-USDT", "KRW-NEW", "BTC-ETH", "USDT-BTC")
    ]
    httpx_mock.add_response(json=rows)
    async with Upbit() as exchange:
        markets = await exchange.fetch_markets()
        for market, row in zip(markets, rows):
            assert market.raw == row
            assert exchange._markets[market.native] is market
            assert market.price_tick is None
            if market.quote != "KRW":
                assert market.amount_step is None
                assert market.min_notional is None
                assert market.public_rules == {}
                continue
            rules = market.public_rules
            assert market.amount_step is None
            assert Decimal(str(market.min_notional)) == Decimal("5000")
            assert rules["amount_step"] == "0.00000001"
            assert rules["min_notional"] == "5000"
            assert rules["min_quantity"] is None
            assert rules["amount_unit"] == market.base
            assert rules["notional_unit"] == market.quote == "KRW"
            assert rules["amount_step_scope"] == "market_buy_fill"
            assert rules["amount_rounding"] == "truncate"
            assert rules["verified_on"] == "2026-09-20"
            assert rules["amount_step_source"] == "https://docs.upbit.com/kr/docs/faq-order"
            assert rules["min_notional_source"] == "https://docs.upbit.com/kr/docs/krw-market-info"
            assert market.model_validate_json(market.model_dump_json()).public_rules == rules
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "api.upbit.com"
    assert requests[0].url.path == "/v1/market/all"
    assert "authorization" not in requests[0].headers


async def test_document_example_and_quote_boundary(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}])
    async with Upbit() as exchange:
        market = (await exchange.fetch_markets())[0]
    rules = market.public_rules
    quantum = Decimal(rules["amount_step"])
    # Official FAQ example: quote budget / price (quote per base) = base quantity.
    quantity = (Decimal("10000") / Decimal("30000")).quantize(quantum, rounding=ROUND_DOWN)
    assert quantity == Decimal("0.33333333")
    assert quantity * Decimal("30000") == Decimal("9999.9999")
    assert Decimal("10000") - quantity * Decimal("30000") == Decimal("0.0001")
    minimum = Decimal(rules["min_notional"])
    assert Decimal("4999.99") < minimum
    assert Decimal("5000") >= minimum
    assert Decimal("100000") >= minimum
    # This is metadata arithmetic, not order execution or a fee policy.


async def test_bithumb_does_not_inherit_upbit_rules(httpx_mock: HTTPXMock) -> None:
    rows = load_fixture("bithumb", "markets")
    httpx_mock.add_response(json=rows)
    async with Bithumb() as exchange:
        markets = await exchange.fetch_markets()
    for market, row in zip(markets, rows):
        assert market.raw == row
        assert market.amount_step is None
        assert market.min_notional is None
        assert market.public_rules == {}


async def test_empty_markets(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[])
    async with Upbit() as exchange:
        assert await exchange.fetch_markets() == []
        assert exchange._markets == {}


async def test_public_error_propagates(httpx_mock: HTTPXMock) -> None:
    from pycex.exceptions import ExchangeError

    httpx_mock.add_response(status_code=400, json={"error": {"name": "bad_request", "message": "bad"}})
    async with Upbit() as exchange:
        with pytest.raises(ExchangeError):
            await exchange.fetch_markets()


# ── price_tick_ladder: docs.upbit.com/kr/docs/krw-market-info (= 2025-07-31 notice "new tick" column) ──

# (price, expected tick): every official floor, and the price just below each floor.
LADDER_CASES = [
    ("0.000001", "0.00000001"),
    ("0.00001", "0.0000001"),
    ("0.000099", "0.0000001"),
    ("0.0001", "0.000001"),
    ("0.001", "0.00001"),
    ("0.01", "0.0001"),
    ("0.1", "0.001"),
    ("0.99", "0.001"),
    ("1", "0.01"),
    ("9.99", "0.01"),
    ("10", "0.1"),
    ("99.9", "0.1"),
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
    ("1999000", "1000"),
    ("2000000", "1000"),
    ("150000000", "1000"),
]


@pytest.mark.parametrize("native", ["KRW-BTC", "KRW-XRP", "KRW-USDT"])
@pytest.mark.parametrize(("price", "tick"), LADDER_CASES)
async def test_krw_ladder_boundaries(httpx_mock: HTTPXMock, native: str, price: str, tick: str) -> None:
    httpx_mock.add_response(json=[{"market": native}])
    async with Upbit() as exchange:
        market = (await exchange.fetch_markets())[0]
    assert tick_for_price(market, price) == Decimal(tick)
    assert market.price_tick is None  # scalar stays unset; consumers read the ladder


async def test_ladder_shares_dict_with_amount_rules_and_survives_roundtrip(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}, {"market": "KRW-ETH"}])
    async with Upbit() as exchange:
        first, second = await exchange.fetch_markets()
    # Same dict as the amount/notional rules (fetch_markets assigns public_rules wholesale).
    assert first.public_rules["amount_step_scope"] == "market_buy_fill"
    assert first.public_rules["price_tick_ladder_source"] == "https://docs.upbit.com/kr/docs/krw-market-info"
    assert len(first.public_rules["price_tick_ladder"]) == 17
    assert tick_ladder(first) == tick_ladder(second)
    # Per-market copies: mutating one market's ladder must not leak into another.
    first.public_rules["price_tick_ladder"].pop()
    assert len(second.public_rules["price_tick_ladder"]) == 17
    assert tick_ladder(type(second).model_validate_json(second.model_dump_json())) == tick_ladder(second)


async def test_ladder_only_on_krw_quote_and_no_limit_quantity_step(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}, {"market": "BTC-ETH"}, {"market": "USDT-BTC"}])
    async with Upbit() as exchange:
        krw, btc, usdt = await exchange.fetch_markets()
    assert btc.public_rules == {} and usdt.public_rules == {}
    assert tick_ladder(btc) is None and tick_ladder(usdt) is None
    # No official limit-order quantity step: the only amount_step is the market-buy fill quantum.
    assert krw.amount_step is None and krw.public_rules["amount_step_scope"] == "market_buy_fill"


async def test_ladder_below_first_floor_is_rejected(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{"market": "KRW-BTC"}])
    async with Upbit() as exchange:
        market = (await exchange.fetch_markets())[0]
    with pytest.raises(ValueError):
        tick_for_price(market, "0")

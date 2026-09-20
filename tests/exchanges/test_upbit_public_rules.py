"""Document-derived KRW rules: units, scope and public-only acquisition."""

from decimal import ROUND_DOWN, Decimal

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.bithumb import Bithumb
from pycex.exchanges.upbit import Upbit
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

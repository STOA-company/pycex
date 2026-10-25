"""Quote budgets must never become base inventory; all HTTP is mocked."""

import json
from decimal import Decimal

import pytest

from pycex.exceptions import InvalidOrderError
from pycex.exchanges.bitget import Bitget
from pycex.exchanges.bybit import Bybit
from pycex.exchanges.okx import OKX


def venue(name, market_type="spot"):
    cls = {"bitget": Bitget, "bybit": Bybit, "okx": OKX}[name]
    kwargs = {"api_key": "test", "secret": "test", "market_type": market_type}
    if name != "bybit":
        kwargs["passphrase"] = "test"
    ex = cls(**kwargs)
    if name == "okx":
        ex._acct_level = "1"
    return ex


def response(name, row, create=False):
    if name == "bybit":
        return {"retCode": 0, "result": row if create else {"list": [row]}}
    return {"code": "00000" if name == "bitget" else "0", "data": [row]}


def order_row(name, state, base=False):
    common = {"side": "buy"}
    if name == "bitget":
        return dict(
            common,
            orderId="1",
            orderType="market",
            size="100.123456789123456789",
            baseVolume="0.002",
            quoteVolume="90",
            status=state,
        )
    if name == "bybit":
        return dict(
            common,
            orderId="1",
            orderType="Market",
            qty="0.003" if base else "100.123456789123456789",
            cumExecQty="0.002",
            cumExecValue="90",
            marketUnit="baseCoin" if base else "quoteCoin",
            orderStatus=state,
        )
    return dict(
        common,
        ordId="1",
        ordType="market",
        sz="0.003" if base else "100.123456789123456789",
        accFillSz="0.002",
        avgPx="45000",
        tgtCcy="base_ccy" if base else "quote_ccy",
        state=state,
    )


@pytest.mark.parametrize("name", ["bitget", "bybit", "okx"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_quote_fetch_units(name, terminal, httpx_mock):
    states = {
        "bitget": ("partially_filled", "cancelled"),
        "bybit": ("PartiallyFilled", "PartiallyFilledCanceled"),
        "okx": ("partially_filled", "canceled"),
    }
    row = order_row(name, states[name][terminal])
    httpx_mock.add_response(json=response(name, row))
    async with venue(name) as ex:
        order = await ex.fetch_order("1", "BTC/USDT")
    assert order.amount == 0
    assert order.amount_exact is None
    assert order.amount_quote == Decimal("100.123456789123456789")
    assert order.filled_exact == Decimal("0.002")
    assert order.cost == Decimal("90")
    assert order.remaining_exact == (Decimal(0) if terminal else None)
    assert order.status == states[name][terminal]
    assert order.raw == row


@pytest.mark.parametrize("name", ["bitget", "bybit", "okx"])
async def test_quote_create_units(name, httpx_mock):
    httpx_mock.add_response(json=response(name, {"orderId": "1", "ordId": "1", "sCode": "0"}, True))
    async with venue(name) as ex:
        order = await ex.create_order("BTC/USDT", "buy", "market", 100)
    body = json.loads(httpx_mock.get_request().content)
    assert order.amount == 0 and order.amount_exact is None
    assert order.amount_quote == Decimal(100)
    assert order.cost is None and order.remaining_exact is None
    if name == "bybit":
        assert body["marketUnit"] == "quoteCoin"
    if name == "okx":
        assert body["tgtCcy"] == "quote_ccy"


@pytest.mark.parametrize("name", ["bybit", "okx"])
async def test_base_market_buy_units(name, httpx_mock):
    httpx_mock.add_response(json=response(name, {"orderId": "1", "ordId": "1", "sCode": "0"}, True))
    state = "PartiallyFilled" if name == "bybit" else "partially_filled"
    httpx_mock.add_response(json=response(name, order_row(name, state, base=True)))
    kwargs = {"market_unit": "baseCoin"} if name == "bybit" else {"tgt_ccy": "base_ccy"}
    async with venue(name) as ex:
        created = await ex.create_order("BTC/USDT", "buy", "market", 0.003, **kwargs)
        fetched = await ex.fetch_order("1", "BTC/USDT")
    assert created.amount_exact == fetched.amount_exact == Decimal("0.003")
    assert created.amount_quote is fetched.amount_quote is None
    assert fetched.remaining_exact == Decimal("0.001")
    assert fetched.cost == Decimal(90)
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["marketUnit" if name == "bybit" else "tgtCcy"] == next(iter(kwargs.values()))


@pytest.mark.parametrize(
    "market_type,order_type,unit",
    [("linear", "market", "baseCoin"), ("spot", "limit", "baseCoin"), ("spot", "market", "bad")],
)
async def test_bybit_invalid_market_unit(market_type, order_type, unit):
    async with venue("bybit", market_type) as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order("BTC/USDT", "buy", order_type, 1, market_unit=unit)


@pytest.mark.parametrize("name", ["bitget", "bybit", "okx"])
@pytest.mark.parametrize("side,kind", [("sell", "market"), ("buy", "limit")])
async def test_base_order_shapes(name, side, kind, httpx_mock):
    row = order_row(name, "filled")
    row["side"] = side
    row[{"bitget": "orderType", "bybit": "orderType", "okx": "ordType"}[name]] = kind
    row[{"bitget": "size", "bybit": "qty", "okx": "sz"}[name]] = "0.003"
    row.pop("marketUnit", None)
    row.pop("tgtCcy", None)
    httpx_mock.add_response(json=response(name, row))
    async with venue(name) as ex:
        order = await ex.fetch_order("1", "BTC/USDT")
    assert order.amount_exact == Decimal("0.003")
    assert order.amount_quote is None
    assert order.filled_exact == Decimal("0.002")
    assert order.remaining_exact == 0


@pytest.mark.parametrize("name", ["bybit", "okx"])
async def test_linear_keeps_base_quantity(name, httpx_mock):
    httpx_mock.add_response(json=response(name, order_row(name, "Filled")))
    async with venue(name, "linear") as ex:
        order = await ex.fetch_order("1", "BTC/USDT:USDT")
    assert order.amount == float("100.123456789123456789")
    assert order.amount_quote is order.cost is order.filled_exact is None

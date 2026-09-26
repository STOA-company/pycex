"""Tests for the Korbit spot adapter (v2 REST, param-based HMAC auth)."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    SymbolNotFoundError,
)
from pycex.exchanges.korbit import (
    Korbit,
    _map_error,
    _parse_candle,
    _parse_market,
    _parse_order,
    _parse_public_trade,
    _parse_ticker,
)
from tests.conftest import load_fixture

SECRET = "test-secret"


def _assert_valid_query_signature(query: str, secret: str = SECRET) -> None:
    """Strip ``signature`` from the real outgoing query string, recompute HMAC-SHA256
    independently (plain ``hmac``, not the adapter's own signing function), and assert
    it matches — a self-referential call to ``korbit_sign`` would be a tautology."""
    pairs = parse_qsl(query, keep_blank_values=True)
    signature = dict(pairs).get("signature")
    assert signature is not None
    message = urlencode([(k, v) for k, v in pairs if k != "signature"])
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


def _assert_valid_form_signature(body: bytes, secret: str = SECRET) -> None:
    _assert_valid_query_signature(body.decode(), secret)


def test_symbol_roundtrip() -> None:
    ex = Korbit()
    assert ex.to_native("BTC/KRW") == "btc_krw"
    assert ex.from_native("btc_krw") == "BTC/KRW"


def test_sandbox_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Korbit(sandbox=True)


def test_linear_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Korbit(market_type="linear")


def test_parse_candle_is_open_time_utc_ms_kst_daily_boundary() -> None:
    raw = load_fixture("korbit", "candles_1d")["data"][0]
    c = _parse_candle(raw)
    assert c.timestamp == raw["timestamp"]
    assert c.open == float(raw["open"]) and c.close == float(raw["close"])
    assert c.volume == float(raw["volume"])
    # Korbit daily bars open at 00:00 KST = 15:00 UTC the previous day (tests/fixtures/NOTES.md).
    assert c.timestamp % 86_400_000 == 15 * 3600 * 1000


def test_parse_market() -> None:
    raw = load_fixture("korbit", "markets")["data"][0]
    m = _parse_market(raw)
    assert m.native == "algo_krw"
    assert m.symbol == "ALGO/KRW"
    assert m.base == "ALGO" and m.quote == "KRW"
    assert m.active is True  # status == "launched"
    assert m.min_notional == 5000.0


def test_parse_market_stopped_is_inactive() -> None:
    raw = load_fixture("korbit", "markets")["data"][2]
    assert raw["status"] == "stopped"
    m = _parse_market(raw)
    assert m.active is False


def test_parse_ticker() -> None:
    raw = load_fixture("korbit", "ticker")["data"][0]
    t = _parse_ticker("BTC/KRW", raw)
    assert t.last == float(raw["close"])
    assert t.bid == float(raw["bestBidPrice"]) and t.ask == float(raw["bestAskPrice"])
    assert t.timestamp == raw["lastTradedAt"]


async def test_candles_page_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("korbit", "candles_1d"))
    ex = Korbit()
    out = await ex._fetch_candles_page("btc_krw", "1d", since=1787842800000, until=1788015600000, limit=3)
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/candles"
    assert req.url.params["symbol"] == "btc_krw"
    assert req.url.params["interval"] == "1D"
    assert req.url.params["start"] == "1787842800000"
    assert req.url.params["end"] == "1788015600000"
    assert req.url.params["limit"] == "3"
    assert out == sorted(out, key=lambda c: c.timestamp)
    await ex.close()


async def test_candles_page_limit_capped_at_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("korbit", "candles_1d"))
    ex = Korbit()
    await ex._fetch_candles_page("btc_krw", "1d", since=None, until=None, limit=500)
    req = httpx_mock.get_request()
    assert req.url.params["limit"] == "200"
    await ex.close()


async def test_fetch_ticker(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("korbit", "ticker"))
    ex = Korbit()
    t = await ex.fetch_ticker("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/tickers" and req.url.params["symbol"] == "btc_krw"
    assert t.symbol == "BTC/KRW" and t.last > 0
    await ex.close()


async def test_fetch_markets(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.korbit.co.kr/v2/currencyPairs", json=load_fixture("korbit", "markets"))
    for native in ("algo_krw", "ens_krw"):  # launched KRW only; kda_krw is stopped
        httpx_mock.add_response(
            url=f"https://api.korbit.co.kr/v2/tickSizePolicy?symbol={native}",
            json=_policy_response(native),
        )
    ex = Korbit()
    markets = await ex.fetch_markets()
    assert len(markets) == 3
    assert {m.symbol for m in markets} == {"ALGO/KRW", "ENS/KRW", "KDA/KRW"}
    assert sorted(r.url.params["symbol"] for r in httpx_mock.get_requests() if r.url.path == "/v2/tickSizePolicy") == [
        "algo_krw",
        "ens_krw",
    ]
    await ex.close()


def _policy_response(native: str) -> dict:
    """The official example recording, re-labelled for ``native`` (same tiers)."""
    body = load_fixture("korbit", "tick_size_policy_xrp_krw")
    body["data"][0]["symbol"] = native
    return body


def test_parse_public_trade_isbuyertaker_true_is_buy() -> None:
    # docs.korbit.co.kr/llms/en/rest_api/quotation.md: isBuyerTaker=true means the
    # taker side of the trade was the buyer -> the trade prints as a taker BUY.
    raw = {"timestamp": 1788015600000, "price": "108519000", "qty": "0.001", "isBuyerTaker": True, "tradeId": 1}
    t = _parse_public_trade("BTC/KRW", raw)
    assert t.side == "buy"


def test_parse_public_trade_isbuyertaker_false_is_sell() -> None:
    raw = {"timestamp": 1788015600000, "price": "108519000", "qty": "0.001", "isBuyerTaker": False, "tradeId": 2}
    t = _parse_public_trade("BTC/KRW", raw)
    assert t.side == "sell"


async def test_fetch_trades(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": [
                {"timestamp": 1788015600000, "price": "108519000", "qty": "0.001", "isBuyerTaker": True, "tradeId": 1},
                {"timestamp": 1788015601000, "price": "108518000", "qty": "0.002", "isBuyerTaker": False, "tradeId": 2},
            ],
        }
    )
    ex = Korbit()
    trades = await ex.fetch_trades("BTC/KRW", limit=50)
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/trades"
    assert req.url.params["symbol"] == "btc_krw" and req.url.params["limit"] == "50"
    assert len(trades) == 2
    assert trades[0].id == "1" and trades[0].side == "buy" and trades[0].price == 108519000.0
    assert trades[1].id == "2" and trades[1].side == "sell" and trades[1].amount == 0.002
    await ex.close()


async def test_fetch_order_book(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {
                "timestamp": 1788015600000,
                "bids": [{"price": "108500000", "qty": "0.5"}, {"price": "108400000", "qty": "1.0"}],
                "asks": [{"price": "108600000", "qty": "0.3"}],
            },
        }
    )
    ex = Korbit()
    ob = await ex.fetch_order_book("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orderbook" and req.url.params["symbol"] == "btc_krw"
    assert ob.symbol == "BTC/KRW"
    assert ob.timestamp == 1788015600000
    assert len(ob.bids) == 2 and ob.bids[0].price == 108500000.0 and ob.bids[0].amount == 0.5
    assert len(ob.asks) == 1 and ob.asks[0].price == 108600000.0 and ob.asks[0].amount == 0.3
    await ex.close()


def test_parse_order_market_buy_amount_from_amt_when_qty_absent() -> None:
    # A market-buy order carries `amt` (quote-currency total), not `qty` — GET /v2/orders
    # response shape (docs.korbit.co.kr/llms/en/rest_api/trading.md).
    raw = {
        "orderId": 42,
        "symbol": "btc_krw",
        "orderType": "market",
        "side": "buy",
        "amt": "100000",
        "filledQty": "0.0009",
        "status": "filled",
        "createdAt": 1788015600000,
    }
    order = _parse_order("BTC/KRW", raw)
    assert order.amount == 100000.0
    assert order.raw["amt"] == "100000"
    assert order.raw.get("qty") is None


async def test_fetch_order_market_buy_uses_amt(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {
                "orderId": 42,
                "symbol": "btc_krw",
                "orderType": "market",
                "side": "buy",
                "amt": "100000",
                "filledQty": "0.0009",
                "status": "filled",
                "createdAt": 1788015600000,
            },
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.fetch_order("42", "BTC/KRW")
    assert order.amount == 100000.0
    await ex.close()


# ── Private / signed ──


async def test_fetch_balance(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": [{"currency": "krw", "balance": "1000000", "available": "900000", "tradeInUse": "100000"}],
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/balance"
    assert req.headers.get("X-KAPI-KEY") == "k"
    _assert_valid_query_signature(req.url.query.decode())
    krw = balance.get("KRW")
    assert krw is not None and krw.free == 900000.0 and krw.locked == 100000.0
    await ex.close()


async def test_create_order_limit_buy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 123}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000)
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders"
    assert req.headers.get("X-KAPI-KEY") == "k"
    body = req.content.decode()
    assert "symbol=btc_krw" in body and "side=buy" in body and "orderType=limit" in body
    assert "qty=0.01" in body and "price=50000000" in body
    _assert_valid_form_signature(req.content)
    assert order.id == "123"
    assert order.side == "buy" and order.type == "limit"
    assert order.amount == 0.01 and order.price == 50_000_000
    await ex.close()


async def test_create_order_market_buy_uses_amt(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 124}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/KRW", "buy", "market", 100_000)
    req = httpx_mock.get_request()
    body = req.content.decode()
    assert "amt=100000" in body
    assert "qty=" not in body
    _assert_valid_form_signature(req.content)
    assert order.side == "buy" and order.type == "market"
    assert order.amount == 100_000 and order.price is None
    await ex.close()


async def test_create_order_market_sell_uses_qty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 125}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/KRW", "sell", "market", 0.02)
    req = httpx_mock.get_request()
    body = req.content.decode()
    assert "qty=0.02" in body
    assert "amt=" not in body
    _assert_valid_form_signature(req.content)
    assert order.side == "sell" and order.type == "market"
    assert order.amount == 0.02 and order.price is None
    await ex.close()


async def test_cancel_order_returns_no_guessed_side_or_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", json={"success": True})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.cancel_order("123", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders"
    assert req.url.params["symbol"] == "btc_krw" and req.url.params["orderId"] == "123"
    _assert_valid_query_signature(req.url.query.decode())
    assert order.id == "123"
    assert order.side == "" and order.type == ""
    await ex.close()


async def test_fetch_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {
                "orderId": 123,
                "symbol": "btc_krw",
                "orderType": "limit",
                "side": "buy",
                "price": "50000000",
                "qty": "0.01",
                "filledQty": "0.0",
                "status": "open",
                "createdAt": 1788015600000,
            },
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.fetch_order("123", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders"
    assert req.url.params["symbol"] == "btc_krw" and req.url.params["orderId"] == "123"
    _assert_valid_query_signature(req.url.query.decode())
    assert order.symbol == "BTC/KRW" and order.side == "buy" and order.type == "limit"
    assert order.amount == 0.01 and order.price == 50_000_000.0
    await ex.close()


async def test_fetch_open_orders(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": [
                {
                    "orderId": 1,
                    "symbol": "btc_krw",
                    "orderType": "limit",
                    "side": "buy",
                    "price": "50000000",
                    "qty": "0.01",
                    "filledQty": "0.0",
                    "status": "open",
                    "createdAt": 1788015600000,
                }
            ],
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    orders = await ex.fetch_open_orders("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/openOrders" and req.url.params["symbol"] == "btc_krw"
    _assert_valid_query_signature(req.url.query.decode())
    assert len(orders) == 1 and orders[0].symbol == "BTC/KRW"
    await ex.close()


async def test_fetch_open_orders_requires_symbol() -> None:
    ex = Korbit(api_key="k", secret=SECRET)
    with pytest.raises(NotSupportedError):
        await ex.fetch_open_orders()
    await ex.close()


async def test_fetch_my_trades(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": [
                {
                    "symbol": "btc_krw",
                    "tradeId": 9,
                    "orderId": 123,
                    "side": "buy",
                    "price": "50000000",
                    "qty": "0.01",
                    "amt": "500000",
                    "tradedAt": 1788015600000,
                    "isTaker": True,
                    "feeCurrency": "krw",
                    "feeQty": "250",
                }
            ],
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    trades = await ex.fetch_my_trades("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/myTrades" and req.url.params["symbol"] == "btc_krw"
    _assert_valid_query_signature(req.url.query.decode())
    assert len(trades) == 1
    t = trades[0]
    assert t.id == "9" and t.order_id == "123" and t.side == "buy"
    assert t.price == 50_000_000.0 and t.amount == 0.01
    assert t.fee == 250.0 and t.fee_asset == "krw"
    await ex.close()


async def test_fetch_my_trades_requires_symbol() -> None:
    ex = Korbit(api_key="k", secret=SECRET)
    with pytest.raises(NotSupportedError):
        await ex.fetch_my_trades()
    await ex.close()


@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        ("NO_BALANCE", InsufficientBalanceError),
        ("ORDER_NOT_FOUND", OrderNotFoundError),
        ("EXCEED_TIME_WINDOW", AuthenticationError),
        ("INVALID_CURRENCY_PAIR", SymbolNotFoundError),
        ("SOME_UNMAPPED_CODE", ExchangeError),
    ],
)
def test_map_error(message: str, expected_type: type[Exception]) -> None:
    exc = _map_error(400, {"error": {"code": 400, "message": message}})
    assert isinstance(exc, expected_type)


def test_map_error_401_403_is_authentication_regardless_of_message() -> None:
    assert isinstance(_map_error(401, {"error": {"code": 401, "message": "ANYTHING"}}), AuthenticationError)
    assert isinstance(_map_error(403, {"error": {"code": 403, "message": "ANYTHING"}}), AuthenticationError)


async def test_success_false_on_http_200_is_mapped(httpx_mock: HTTPXMock) -> None:
    """Korbit can return `{"success": false, ...}` on an HTTP 200 status — the
    HTTPClient error_mapper only inspects status >= 400, so `_unwrap` must catch this."""
    httpx_mock.add_response(status_code=200, json={"success": False, "error": {"code": 400, "message": "NO_BALANCE"}})
    ex = Korbit(api_key="k", secret=SECRET)
    with pytest.raises(InsufficientBalanceError):
        await ex.fetch_balance()
    await ex.close()


# ── client_order_id: POST/GET /v2/orders `clientOrderId`, regex [0-9a-zA-Z.:_-]{1,36} (docs trading.md) ──

CID = "qtx-0123456789abcdef.:_-ABCDEF01"  # 33 chars, every allowed character class


async def test_create_order_sends_signed_client_order_id_and_echoes_it(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 200}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=CID)
    req = httpx_mock.get_request()
    fields = dict(parse_qsl(req.content.decode()))
    assert fields["clientOrderId"] == CID
    _assert_valid_form_signature(req.content)  # clientOrderId is inside the signed string
    assert order.id == "200" and order.client_order_id == CID
    await ex.close()


async def test_create_order_without_client_order_id_sends_no_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 201}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/KRW", "sell", "market", 0.02)
    assert "clientOrderId" not in httpx_mock.get_request().content.decode()
    assert order.client_order_id is None
    await ex.close()


@pytest.mark.parametrize("bad", ["", "x" * 37, "has space", "slash/no", "한글"])
async def test_create_order_rejects_invalid_client_order_id_without_request(httpx_mock: HTTPXMock, bad: str) -> None:
    ex = Korbit(api_key="k", secret=SECRET)
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=bad)
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_create_order_accepts_36_char_client_order_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json={"success": True, "data": {"orderId": 202}})
    ex = Korbit(api_key="k", secret=SECRET)
    await ex.create_order("BTC/KRW", "buy", "market", 10_000, client_order_id="x" * 36)
    assert dict(parse_qsl(httpx_mock.get_request().content.decode()))["clientOrderId"] == "x" * 36
    await ex.close()


async def test_fetch_order_by_client_order_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {
                "orderId": 200,
                "clientOrderId": CID,
                "symbol": "btc_krw",
                "orderType": "limit",
                "side": "buy",
                "qty": "0.01",
                "price": "50000000",
                "status": "filled",
                "createdAt": 1788015600000,
            },
        }
    )
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CID)
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders"
    assert req.url.params["clientOrderId"] == CID and "orderId" not in req.url.params
    _assert_valid_query_signature(req.url.query.decode())
    assert order.id == "200" and order.client_order_id == CID
    await ex.close()


async def test_fetch_order_by_order_id_still_sends_order_id_only(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"success": True, "data": {"orderId": 200, "symbol": "btc_krw"}})
    ex = Korbit(api_key="k", secret=SECRET)
    order = await ex.fetch_order("200", "BTC/KRW")
    params = httpx_mock.get_request().url.params
    assert params["orderId"] == "200" and "clientOrderId" not in params
    assert order.client_order_id is None
    await ex.close()


@pytest.mark.parametrize(("order_id", "client_id"), [(None, None), ("", ""), ("200", CID)])
async def test_fetch_order_requires_exactly_one_key_without_request(
    httpx_mock: HTTPXMock, order_id: str | None, client_id: str | None
) -> None:
    ex = Korbit(api_key="k", secret=SECRET)
    with pytest.raises(InvalidOrderError):
        await ex.fetch_order(order_id, "BTC/KRW", client_order_id=client_id)
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_open_orders_echo_client_order_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"success": True, "data": [{"orderId": 200, "clientOrderId": CID, "symbol": "btc_krw"}]}
    )
    ex = Korbit(api_key="k", secret=SECRET)
    assert (await ex.fetch_open_orders("BTC/KRW"))[0].client_order_id == CID
    await ex.close()

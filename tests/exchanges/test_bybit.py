"""Tests for the Bybit V5 adapter: linear symbol handling, exact-body request
signing, markets/fills, and unified error mapping.

Endpoint/field shapes (``market/instruments-info``, ``execution/list``) and
the auth sign-string formula were live-fetched and confirmed against the
official docs (2026-08-30) — see the module docstring in
``src/pycex/exchanges/bybit.py`` for the full breakdown and sources.
"""

from __future__ import annotations

import hashlib
import hmac
import json as json_lib

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    AuthenticationError,
    InsufficientBalanceError,
    NotSupportedError,
    OrderNotFoundError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.exchanges.bybit import Bybit, _map_error, _parse_market, _parse_my_trade

SECRET = "s"
RECV_WINDOW = "5000"


def _assert_valid_post_signature(request: httpx.Request, secret: str = SECRET) -> None:
    """Recompute HMAC-SHA256 independently (plain ``hmac``, not the adapter's
    own ``bybit_headers``) over the real outgoing request — timestamp/
    recv_window from the sent ``X-BAPI-TIMESTAMP``/``X-BAPI-RECV-WINDOW``
    headers, body from the actual request content — and assert it matches.
    Catches a body that was signed as one string but sent as a re-serialized
    one (e.g. ``post()``'s ``json=`` vs. ``post_raw()``'s verbatim
    ``content=``): signing ``json.dumps(body)`` and then sending via
    ``post(data=body)`` would re-encode the dict with httpx's own JSON
    separators, breaking this exact check.
    """
    ts = request.headers["X-BAPI-TIMESTAMP"]
    recv_window = request.headers["X-BAPI-RECV-WINDOW"]
    api_key = request.headers["X-BAPI-API-KEY"]
    body = request.content.decode() if request.content else ""
    message = ts + api_key + recv_window + body
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected


def _assert_valid_get_signature(request: httpx.Request, secret: str = SECRET) -> None:
    """Same as :func:`_assert_valid_post_signature`, but for GET: the prehash
    uses the real outgoing **query string** (``request.url.query``), not the
    body."""
    ts = request.headers["X-BAPI-TIMESTAMP"]
    recv_window = request.headers["X-BAPI-RECV-WINDOW"]
    api_key = request.headers["X-BAPI-API-KEY"]
    query = request.url.query.decode()
    message = ts + api_key + recv_window + query
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected


# ── Symbols ──


def test_to_native_spot_symbol() -> None:
    ex = Bybit(api_key="k", secret=SECRET)
    assert ex.to_native("BTC/USDT") == "BTCUSDT"


def test_to_native_linear_symbol_strips_settle() -> None:
    ex = Bybit(api_key="k", secret=SECRET, market_type="linear")
    assert ex.to_native("BTC/USDT:USDT") == "BTCUSDT"


def test_to_native_inverse_settle_rejected() -> None:
    ex = Bybit(api_key="k", secret=SECRET, market_type="linear")
    with pytest.raises(SymbolNotFoundError):
        ex.to_native("BTC/USD:BTC")


def test_from_native_linear_fallback_no_cache_produces_settle() -> None:
    ex = Bybit(api_key="k", secret=SECRET, market_type="linear")
    assert ex.from_native("BTCUSDT") == "BTC/USDT:USDT"


def test_from_native_spot_fallback_no_cache_never_emits_settle() -> None:
    ex = Bybit(api_key="k", secret=SECRET, market_type="spot")
    assert ex.from_native("BTCUSDT") == "BTC/USDT"


def test_symbol_round_trip_linear() -> None:
    ex = Bybit(api_key="k", secret=SECRET, market_type="linear")
    native = ex.to_native("BTC/USDT:USDT")
    assert ex.from_native(native) == "BTC/USDT:USDT"


# ── fetch_markets ──


def test_parse_market_spot() -> None:
    """Real spot ``lotSizeFilter`` shape (confirmed via WebFetch, 2026-08-30):
    ``basePrecision``/``minOrderQty``/``minOrderAmt`` — no ``qtyStep`` (that
    field only exists on linear rows)."""
    raw = {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.01"},
        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000048", "minOrderAmt": "1"},
    }
    m = _parse_market(raw, "spot")
    assert m is not None
    assert m.symbol == "BTC/USDT"
    assert m.native == "BTCUSDT"
    assert m.price_tick == 0.01
    assert m.amount_step == float(raw["lotSizeFilter"]["basePrecision"])
    assert m.min_notional == 1.0
    assert m.active is True


def test_parse_market_linear_derives_settle_symbol() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.5"},
        "lotSizeFilter": {"qtyStep": "0.001", "minNotionalValue": "5"},
    }
    m = _parse_market(raw, "linear")
    assert m is not None
    assert m.symbol == "BTC/USDT:USDT"
    assert m.amount_step == 0.001
    assert m.min_notional == 5.0


def test_parse_market_linear_skips_non_usdt_settle() -> None:
    """Only USDT-settled linear contracts are in scope for this phase
    (matching ``to_native``'s rejection of inverse/cross-settle symbols) —
    a row whose ``settleCoin`` isn't the quote currency must be skipped
    rather than silently mislabeled as USDT-settled."""
    raw = {
        "symbol": "BTCUSD",
        "baseCoin": "BTC",
        "quoteCoin": "USD",
        "settleCoin": "BTC",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.5"},
        "lotSizeFilter": {"qtyStep": "1"},
    }
    assert _parse_market(raw, "linear") is None


def test_parse_market_inactive_status() -> None:
    raw = {"symbol": "XUSDT", "baseCoin": "X", "quoteCoin": "USDT", "status": "Delisted"}
    m = _parse_market(raw, "spot")
    assert m is not None
    assert m.active is False


async def test_fetch_markets_spot_populates_cache(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "spot",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "status": "Trading",
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderAmt": "1"},
                    }
                ],
            },
        }
    )
    ex = Bybit(api_key="k", secret=SECRET)
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.params.get("category") == "spot"
    assert len(markets) == 1
    assert markets[0].amount_step == 0.000001
    assert ex.from_native("BTCUSDT") == "BTC/USDT"
    await ex.close()


async def test_fetch_markets_linear_populates_cache_and_symbol(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "priceFilter": {"tickSize": "0.5"},
                        "lotSizeFilter": {"qtyStep": "0.001", "minNotionalValue": "5"},
                    }
                ],
            },
        }
    )
    ex = Bybit(api_key="k", secret=SECRET, market_type="linear")
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.params.get("category") == "linear"
    assert markets[0].symbol == "BTC/USDT:USDT"
    assert markets[0].min_notional == 5.0
    assert ex.from_native("BTCUSDT") == "BTC/USDT:USDT"
    await ex.close()


# ── fetch_my_trades ──


def test_parse_my_trade() -> None:
    raw = {
        "execId": "e1",
        "orderId": "o1",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "execPrice": "65000",
        "execQty": "0.01",
        "execFee": "0.65",
        "feeCurrency": "USDT",
        "execTime": "1700000000000",
    }
    t = _parse_my_trade("BTC/USDT", raw)
    assert t.id == "e1"
    assert t.order_id == "o1"
    assert t.side == "buy"
    assert t.price == 65000.0
    assert t.amount == 0.01
    assert t.fee == 0.65
    assert t.fee_asset == "USDT"
    assert t.timestamp == 1700000000000


async def test_fetch_my_trades_sends_category_and_symbol(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "spot",
                "list": [
                    {
                        "execId": "e1",
                        "orderId": "o1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execPrice": "65000",
                        "execQty": "0.01",
                        "execFee": "0.65",
                        "feeCurrency": "USDT",
                        "execTime": "1700000000000",
                    }
                ],
            },
        }
    )
    ex = Bybit(api_key="k", secret=SECRET)
    trades = await ex.fetch_my_trades("BTC/USDT")
    req = httpx_mock.get_request()
    assert req.url.params.get("category") == "spot"
    assert req.url.params.get("symbol") == "BTCUSDT"
    assert len(trades) == 1
    assert trades[0].symbol == "BTC/USDT"
    await ex.close()


async def test_fetch_my_trades_no_symbol_resolves_via_from_native(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "spot",
                "list": [
                    {
                        "execId": "e1",
                        "orderId": "o1",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "execPrice": "65000",
                        "execQty": "0.01",
                        "execFee": "0.65",
                        "feeCurrency": "USDT",
                        "execTime": "1700000000000",
                    }
                ],
            },
        }
    )
    ex = Bybit(api_key="k", secret=SECRET)
    trades = await ex.fetch_my_trades()
    assert trades[0].symbol == "BTC/USDT"
    await ex.close()


async def test_fetch_my_trades_since_filters_client_side(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "spot",
                "list": [
                    {
                        "execId": "e1",
                        "orderId": "o1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execPrice": "1",
                        "execQty": "1",
                        "execFee": "0",
                        "feeCurrency": "USDT",
                        "execTime": "1000",
                    },
                    {
                        "execId": "e2",
                        "orderId": "o2",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execPrice": "1",
                        "execQty": "1",
                        "execFee": "0",
                        "feeCurrency": "USDT",
                        "execTime": "5000",
                    },
                ],
            },
        }
    )
    ex = Bybit(api_key="k", secret=SECRET)
    trades = await ex.fetch_my_trades("BTC/USDT", since=2000)
    assert [t.id for t in trades] == ["e2"]
    await ex.close()


# ── create_order/cancel_order: exact-body signature ──


async def test_create_order_sends_content_type_and_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}})
    ex = Bybit(api_key="k", secret=SECRET)
    order = await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    req = httpx_mock.get_request()
    assert req.url.path == "/v5/order/create"
    assert req.headers["Content-Type"] == "application/json"
    body = json_lib.loads(req.content.decode())
    assert body["symbol"] == "BTCUSDT"
    assert body["side"] == "Buy"
    assert body["orderType"] == "Limit"
    assert order.amount == 0.001
    assert order.price == 50000.0
    assert order.side == "buy"
    assert order.type == "limit"
    await ex.close()


async def test_create_order_rejects_unsupported_futures_options(httpx_mock: HTTPXMock) -> None:
    ex = Bybit(api_key="k", secret=SECRET)
    with pytest.raises(NotSupportedError):
        await ex.create_order("BTC/USDT", "buy", "market", 0.001, reduce_only=True)
    with pytest.raises(NotSupportedError):
        await ex.create_order("BTC/USDT", "buy", "market", 0.001, client_order_id="client-1")
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_create_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    """The prehash must be computed over the exact bytes sent on the wire,
    not a dict that gets re-serialized separately by httpx — otherwise every
    real order would fail Bybit's signature check. Recomputes independently
    from the real captured request rather than calling the adapter's own
    signer (a self-referential call would be a tautology)."""
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}})
    ex = Bybit(api_key="k", secret=SECRET)
    await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    req = httpx_mock.get_request()
    _assert_valid_post_signature(req)
    await ex.close()


async def test_cancel_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}})
    ex = Bybit(api_key="k", secret=SECRET)
    order = await ex.cancel_order("1", "BTC/USDT")
    req = httpx_mock.get_request()
    assert req.headers["Content-Type"] == "application/json"
    assert order.side == ""
    assert order.type == ""
    _assert_valid_post_signature(req)
    await ex.close()


async def test_fetch_balance_get_signature_matches_query_string(httpx_mock: HTTPXMock) -> None:
    """GET requests sign the real outgoing query string, not a body — this
    would also catch a query-encoding mismatch between what was signed and
    what httpx actually sends on the wire."""
    httpx_mock.add_response(
        json={"retCode": 0, "retMsg": "OK", "result": {"list": [{"coin": []}]}},
    )
    ex = Bybit(api_key="k", secret=SECRET)
    await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/v5/account/wallet-balance"
    _assert_valid_get_signature(req)
    await ex.close()


async def test_fetch_my_trades_multi_param_signature_matches_query_string(httpx_mock: HTTPXMock) -> None:
    """Regression: with both ``symbol`` and ``limit`` set, a signer that
    signs a differently-ordered (e.g. alphabetically sorted) query string
    than the one actually sent on the wire produces a signature Bybit would
    reject on every real request — a single-param request (e.g. balance's
    ``accountType`` only) can't catch this, so this needs >= 2 params."""
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": []}})
    ex = Bybit(api_key="k", secret=SECRET)
    await ex.fetch_my_trades("BTC/USDT", limit=10)
    req = httpx_mock.get_request()
    assert req.url.path == "/v5/execution/list"
    assert req.url.params.get("category") == "spot"
    assert req.url.params.get("symbol") == "BTCUSDT"
    assert req.url.params.get("limit") == "10"
    _assert_valid_get_signature(req)
    await ex.close()


async def test_fetch_open_orders_multi_param_signature_matches_query_string(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": []}})
    ex = Bybit(api_key="k", secret=SECRET)
    await ex.fetch_open_orders("BTC/USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/v5/order/realtime"
    assert req.url.params.get("category") == "spot"
    assert req.url.params.get("symbol") == "BTCUSDT"
    _assert_valid_get_signature(req)
    await ex.close()


# ── error mapping ──


def test_map_error_insufficient_balance() -> None:
    err = _map_error("110007", "Available balance is insufficient")
    assert isinstance(err, InsufficientBalanceError)


def test_map_error_authentication() -> None:
    err = _map_error("10003", "API key is invalid")
    assert isinstance(err, AuthenticationError)


def test_map_error_order_not_found() -> None:
    err = _map_error("110001", "Order does not exist")
    assert isinstance(err, OrderNotFoundError)


def test_map_error_rate_limit() -> None:
    err = _map_error("10006", "Too many visits")
    assert isinstance(err, RateLimitError)


async def test_map_error_on_retcode_body_http_200(httpx_mock: HTTPXMock) -> None:
    """Bybit's normal error posture: HTTP 200 with a non-zero ``retCode`` —
    handled by ``Bybit._check``, independent of ``HTTPClient``'s error_mapper
    hook (which only fires for HTTP>=400 bodies)."""
    httpx_mock.add_response(json={"retCode": 110007, "retMsg": "Available balance is insufficient", "result": {}})
    ex = Bybit(api_key="k", secret=SECRET)
    with pytest.raises(InsufficientBalanceError):
        await ex.fetch_balance()
    await ex.close()


async def test_map_error_on_http_400_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"retCode": 10004, "retMsg": "Error sign"})
    ex = Bybit(api_key="k", secret=SECRET)
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    await ex.close()


async def test_candles_page_is_sorted_ascending(httpx_mock: HTTPXMock) -> None:
    """Bybit's kline list is newest-first; the unified contract is ascending, and
    the page hook is what has to fix that (the base class only sorts the
    multi-page path)."""
    rows = [
        ["1788048000000", "3", "3", "3", "3", "1", "1"],
        ["1787961600000", "2", "2", "2", "2", "1", "1"],
        ["1787875200000", "1", "1", "1", "1", "1", "1"],
    ]
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "OK", "result": {"list": rows}})
    ex = Bybit()
    candles = await ex.fetch_candles("BTC/USDT", "1d", limit=3)
    assert [c.timestamp for c in candles] == [1787875200000, 1787961600000, 1788048000000]
    assert [c.close for c in candles] == [1.0, 2.0, 3.0]
    await ex.close()

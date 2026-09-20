"""Tests for the Upbit spot adapter (reference implementation for KRW exchanges)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import pytest
from pytest_httpx import HTTPXMock

from pycex.auth import jwt_hs256
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InsufficientBalanceError,
    NotSupportedError,
    OrderNotFoundError,
    SymbolNotFoundError,
)
from pycex.exchanges.upbit import Upbit, _map_error, _parse_candle, _parse_market, _parse_ticker
from tests.conftest import load_fixture


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    result: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return result


def _assert_bearer_query_hash(headers: Any, params: dict[str, Any]) -> None:
    auth = headers.get("Authorization", "")
    assert auth.startswith("Bearer ")
    payload = _decode_jwt_payload(auth[len("Bearer ") :])
    assert payload["query_hash_alg"] == "SHA512"
    assert payload["query_hash"] == hashlib.sha512(urlencode(params, doseq=True).encode()).hexdigest()


def test_symbol_roundtrip() -> None:
    ex = Upbit()
    assert ex.to_native("BTC/KRW") == "KRW-BTC"
    assert ex.from_native("KRW-BTC") == "BTC/KRW"


def test_linear_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Upbit(market_type="linear")


def test_sandbox_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Upbit(sandbox=True)


def test_parse_candle_uses_utc_open_time() -> None:
    raw = load_fixture("upbit", "candles_1d")[0]
    c = _parse_candle(raw)
    # candle_date_time_utc "YYYY-MM-DDTHH:MM:SS" → epoch ms; 업비트 timestamp 필드(마지막 체결)는 쓰지 않는다
    expected = int(datetime.fromisoformat(raw["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    assert c.timestamp == expected
    assert c.timestamp != raw["timestamp"]
    assert c.open == raw["opening_price"] and c.close == raw["trade_price"]
    assert c.volume == raw["candle_acc_trade_volume"]


def test_parse_market() -> None:
    m = _parse_market(load_fixture("upbit", "markets")[0])
    assert m.native.startswith(("KRW-", "BTC-", "USDT-"))
    assert m.symbol == f"{m.base}/{m.quote}"


def test_parse_ticker() -> None:
    t = _parse_ticker(load_fixture("upbit", "ticker")[0])
    assert t.last > 0


def test_jwt_hs256_shape() -> None:
    tok = jwt_hs256("secret", {"access_key": "k", "nonce": "n"})
    assert tok.count(".") == 2
    hdr = json.loads(base64.urlsafe_b64decode(tok.split(".")[0] + "=="))
    assert hdr == {"alg": "HS256", "typ": "JWT"}


def test_jwt_hs256_signature_matches_known_vector() -> None:
    # Verify against an independently recomputed HMAC-SHA256 over header.payload.
    tok = jwt_hs256("s", {"access_key": "k", "nonce": "n"})
    header_b64, payload_b64, sig_b64 = tok.split(".")
    expected_sig = hmac.new(b"s", f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()
    assert sig_b64 == expected_sig_b64


async def test_candles_page_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("upbit", "candles_1d"))
    ex = Upbit()
    out = await ex._fetch_candles_page("KRW-BTC", "1d", since=None, until=None, limit=3)
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/candles/days" and req.url.params["count"] == "3"
    assert out == sorted(out, key=lambda c: c.timestamp)  # 오름차순으로 뒤집었는가
    await ex.close()


def test_parse_market_active_even_when_warned() -> None:
    """Delisting removes a market from the list entirely — a still-listed market with
    an investor-warning flag must still be reported as active; the flag is preserved
    in `raw` for callers who want to surface it."""
    d = {
        "market": "KRW-XYZ",
        "korean_name": "테스트",
        "english_name": "Test",
        "market_event": {"warning": True, "caution": {}},
    }
    m = _parse_market(d)
    assert m.active is True
    assert m.raw["market_event"]["warning"] is True


def _order_response(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "uuid": "order-uuid-1",
        "side": "bid",
        "ord_type": "limit",
        "price": "50000000.0",
        "state": "wait",
        "market": "KRW-BTC",
        "created_at": "2026-08-30T00:00:00+09:00",
        "volume": "0.01",
        "remaining_volume": "0.01",
        "executed_volume": "0.0",
    }
    base.update(overrides)
    return base


async def test_create_order_limit_buy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_order_response())
    ex = Upbit(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000)
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/orders"
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "bid", "ord_type": "limit", "volume": "0.01", "price": "50000000"}
    assert body == expected_body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 0.01 and order.price == 50_000_000
    await ex.close()


async def test_create_order_rejects_unsupported_futures_options(httpx_mock: HTTPXMock) -> None:
    ex = Upbit(api_key="k", secret="s")
    with pytest.raises(NotSupportedError):
        await ex.create_order("BTC/KRW", "buy", "market", 100_000, reduce_only=True)
    with pytest.raises(NotSupportedError):
        await ex.create_order("BTC/KRW", "buy", "market", 100_000, client_order_id="client-1")
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_create_order_market_buy_uses_price_ord_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_order_response(ord_type="price", price="100000.0", volume=None))
    ex = Upbit(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "market", 100_000)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "100000"}
    assert body == expected_body
    assert "volume" not in body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 100_000 and order.price is None  # echoes the request, not the response
    await ex.close()


async def test_create_order_market_sell_uses_market_ord_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", json=_order_response(side="ask", ord_type="market", price=None, volume="0.02")
    )
    ex = Upbit(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "sell", "market", 0.02)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "ask", "ord_type": "market", "volume": "0.02"}
    assert body == expected_body
    assert "price" not in body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 0.02 and order.price is None
    await ex.close()


async def test_cancel_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", json=_order_response(state="cancel"))
    ex = Upbit(api_key="k", secret="s")
    order = await ex.cancel_order("order-uuid-1", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/order" and req.url.params["uuid"] == "order-uuid-1"
    _assert_bearer_query_hash(req.headers, {"uuid": "order-uuid-1"})
    assert order.id == "order-uuid-1"
    await ex.close()


async def test_fetch_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=_order_response())
    ex = Upbit(api_key="k", secret="s")
    order = await ex.fetch_order("order-uuid-1", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/order" and req.url.params["uuid"] == "order-uuid-1"
    _assert_bearer_query_hash(req.headers, {"uuid": "order-uuid-1"})
    assert order.symbol == "BTC/KRW"
    await ex.close()


async def test_fetch_open_orders(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=[_order_response()])
    ex = Upbit(api_key="k", secret="s")
    orders = await ex.fetch_open_orders("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/orders"
    assert req.url.params["state"] == "wait" and req.url.params["market"] == "KRW-BTC"
    expected_params = {"state": "wait", "market": "KRW-BTC"}
    _assert_bearer_query_hash(req.headers, expected_params)
    assert len(orders) == 1
    assert orders[0].symbol == "BTC/KRW"
    await ex.close()


async def test_fetch_my_trades_flattens_two_orders(httpx_mock: HTTPXMock) -> None:
    response = [
        {
            "uuid": "order-1",
            "side": "bid",
            "market": "KRW-BTC",
            "created_at": "2026-08-30T00:00:00+09:00",
            "state": "done",
            "trades": [{"market": "KRW-BTC", "uuid": "trade-1", "price": "100000000.0", "volume": "0.001"}],
        },
        {
            "uuid": "order-2",
            "side": "ask",
            "market": "KRW-BTC",
            "created_at": "2026-08-30T01:00:00+09:00",
            "state": "done",
            "trades": [{"market": "KRW-BTC", "uuid": "trade-2", "price": "101000000.0", "volume": "0.002"}],
        },
    ]
    httpx_mock.add_response(method="GET", json=response)
    ex = Upbit(api_key="k", secret="s")
    trades = await ex.fetch_my_trades("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.params["state"] == "done" and req.url.params["market"] == "KRW-BTC"
    expected_params = {"state": "done", "limit": 100, "market": "KRW-BTC"}
    _assert_bearer_query_hash(req.headers, expected_params)
    assert len(trades) == 2
    assert trades[0].id == "trade-1" and trades[0].order_id == "order-1" and trades[0].fee_asset == "KRW"
    assert trades[1].id == "trade-2" and trades[1].order_id == "order-2" and trades[1].fee_asset == "KRW"
    await ex.close()


async def test_fetch_balance(httpx_mock: HTTPXMock) -> None:
    response = [
        {"currency": "KRW", "balance": "1000000.0", "locked": "0.0"},
        {"currency": "BTC", "balance": "0.5", "locked": "0.1"},
    ]
    httpx_mock.add_response(method="GET", json=response)
    ex = Upbit(api_key="k", secret="s")
    balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/accounts"
    auth = req.headers.get("Authorization", "")
    assert auth.startswith("Bearer ")
    payload = _decode_jwt_payload(auth[len("Bearer ") :])
    assert "query_hash" not in payload  # no query params on this call
    krw = balance.get("KRW")
    assert krw is not None
    assert krw.free == 1000000.0
    await ex.close()


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("insufficient_funds_bid", InsufficientBalanceError),
        ("jwt_verification", AuthenticationError),
        ("order_not_found", OrderNotFoundError),
        ("some_unmapped_error", ExchangeError),
    ],
)
def test_map_error(name: str, expected_type: type[Exception]) -> None:
    exc = _map_error(400, {"error": {"name": name, "message": "boom"}})
    assert isinstance(exc, expected_type)


def test_map_error_int_name_is_not_a_crash() -> None:
    """Upbit returns an **int** ``name`` for its 404 envelope (live probe 2026-08-30:
    ``GET /v1/ticker?markets=KRW-NOPE`` -> HTTP 404
    ``{"error":{"name":404,"message":"Code not found"}}``). Substring checks on the
    name used to raise ``TypeError: argument of type 'int' is not iterable``."""
    exc = _map_error(404, {"error": {"name": 404, "message": "Code not found"}})
    assert isinstance(exc, SymbolNotFoundError)


def test_map_error_invalid_jwt_is_authentication_error() -> None:
    """Live probe 2026-08-30: ``GET /v1/accounts`` with a garbage bearer token ->
    HTTP 401 ``{"error":{"name":"invalid_jwt"}}``."""
    assert isinstance(_map_error(401, {"error": {"name": "invalid_jwt"}}), AuthenticationError)


async def test_fetch_ticker_unknown_market_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=404, json={"error": {"name": 404, "message": "Code not found"}})
    ex = Upbit()
    with pytest.raises(SymbolNotFoundError):
        await ex.fetch_ticker("NOPE/KRW")
    await ex.close()


async def test_error_envelope_with_http_200_still_raises(httpx_mock: HTTPXMock) -> None:
    """A KRW-v1 error envelope must raise regardless of the HTTP status — the
    HTTPClient error_mapper only runs for status >= 400."""
    httpx_mock.add_response(status_code=200, json={"error": {"name": "some_unmapped_error", "message": "boom"}})
    ex = Upbit()
    with pytest.raises(ExchangeError):
        await ex.fetch_order_book("BTC/KRW")
    await ex.close()


def test_format_to_is_utc_with_z_suffix() -> None:
    """Upbit reads a naive `to` as UTC and accepts the `Z` suffix (live probe
    2026-08-30: ``to=2026-08-25T00:00:00Z`` and ``to=2026-08-25T00:00:00`` both
    return the 2026-08-24T00:00:00 bar as the newest)."""
    assert Upbit()._format_to(1_787_616_000_000) == "2026-08-25T00:00:00Z"


async def test_candles_page_to_param_is_utc(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("upbit", "candles_1d"))
    ex = Upbit()
    await ex._fetch_candles_page("KRW-BTC", "1d", since=None, until=1_787_615_999_999, limit=3)
    assert httpx_mock.get_request().url.params["to"] == "2026-08-25T00:00:00Z"
    await ex.close()

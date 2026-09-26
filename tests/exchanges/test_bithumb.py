"""Tests for the Bithumb spot adapter (v1/v2 mixed, shares public API with Upbit)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

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
from pycex.exchanges.bithumb import Bithumb, _map_error, _parse_candle, _parse_market, _parse_ticker
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
    assert "timestamp" in payload  # Bithumb-specific: required on every payload, unlike Upbit
    assert payload["query_hash_alg"] == "SHA512"
    assert payload["query_hash"] == hashlib.sha512(urlencode(params, doseq=True).encode()).hexdigest()


def test_symbol_roundtrip() -> None:
    ex = Bithumb()
    assert ex.to_native("BTC/KRW") == "KRW-BTC"
    assert ex.from_native("KRW-BTC") == "BTC/KRW"


def test_linear_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Bithumb(market_type="linear")


def test_sandbox_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        Bithumb(sandbox=True)


def test_parse_candle_uses_utc_open_time_and_kst_daily_boundary() -> None:
    raw = load_fixture("bithumb", "candles_1d")[0]
    c = _parse_candle(raw)
    expected = int(datetime.fromisoformat(raw["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    assert c.timestamp == expected
    assert c.timestamp != raw["timestamp"]
    assert c.open == raw["opening_price"] and c.close == raw["trade_price"]
    assert c.volume == raw["candle_acc_trade_volume"]
    # 🚨 Bithumb's daily bars open at 00:00 KST = 15:00 UTC the previous day —
    # different from Upbit, which opens at 00:00 UTC. See tests/fixtures/NOTES.md.
    assert c.timestamp % 86_400_000 == 15 * 3600 * 1000


def test_parse_market() -> None:
    m = _parse_market(load_fixture("bithumb", "markets")[0])
    assert m.native.startswith(("KRW-", "BTC-", "USDT-"))
    assert m.symbol == f"{m.base}/{m.quote}"


def test_parse_ticker() -> None:
    t = _parse_ticker(load_fixture("bithumb", "ticker")[0])
    assert t.last > 0


def test_jwt_payload_has_timestamp() -> None:
    from pycex.auth import bithumb_headers

    tok = bithumb_headers("k", "s", None)["Authorization"].split()[1]
    payload = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
    assert "timestamp" in payload and payload["access_key"] == "k"


async def test_candles_page_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bithumb", "candles_1d"))
    ex = Bithumb()
    out = await ex._fetch_candles_page("KRW-BTC", "1d", since=None, until=None, limit=3)
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/candles/days" and req.url.params["count"] == "3"
    assert out == sorted(out, key=lambda c: c.timestamp)
    await ex.close()


def _full_order_response(**overrides: Any) -> dict[str, Any]:
    """Shape returned by GET /v1/order (and, after v2->v1 field normalization,
    GET /v2/orders/pending|/history entries)."""
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


def _v2_create_response(**overrides: Any) -> dict[str, Any]:
    """Real Bithumb POST /v2/orders response shape — no price/volume/state."""
    base: dict[str, Any] = {
        "order_id": "order-uuid-1",
        "market": "KRW-BTC",
        "side": "bid",
        "order_type": "limit",
        "created_at": "2026-08-30T00:00:00+09:00",
        "stp_type": "cancel_taker",
    }
    base.update(overrides)
    return base


async def test_create_order_limit_buy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response())
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000)
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders"
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "bid", "order_type": "limit", "volume": "0.01", "price": "50000000"}
    assert body == expected_body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 0.01 and order.price == 50_000_000
    await ex.close()


async def test_create_order_market_buy_uses_price_order_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response(order_type="price"))
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "market", 100_000)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "bid", "order_type": "price", "price": "100000"}
    assert body == expected_body
    assert "volume" not in body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 100_000 and order.price is None  # echoes the request, not the (partial) response
    await ex.close()


async def test_create_order_market_sell_uses_market_order_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response(side="ask", order_type="market"))
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "sell", "market", 0.02)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    expected_body = {"market": "KRW-BTC", "side": "ask", "order_type": "market", "volume": "0.02"}
    assert body == expected_body
    assert "price" not in body
    _assert_bearer_query_hash(req.headers, expected_body)
    assert order.symbol == "BTC/KRW"
    assert order.amount == 0.02 and order.price is None
    await ex.close()


async def test_cancel_order(httpx_mock: HTTPXMock) -> None:
    # Real DELETE /v2/order response: no side/type/price at all.
    cancel_response = {"order_id": "order-uuid-1", "created_at": "2026-08-30T00:00:00+09:00"}
    httpx_mock.add_response(method="DELETE", json=cancel_response)
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.cancel_order("order-uuid-1", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/order" and req.url.params["order_id"] == "order-uuid-1"
    _assert_bearer_query_hash(req.headers, {"order_id": "order-uuid-1"})
    assert order.id == "order-uuid-1"
    assert order.status == "cancel"
    # cancel_order only knows order_id/symbol — it cannot know the original side/type,
    # and the sparse v2 response doesn't carry them either, so both must stay unguessed.
    assert order.side == "" and order.type == ""
    await ex.close()


def test_parse_order_does_not_fabricate_side_or_type_for_sparse_response() -> None:
    """The real DELETE /v2/order response shape carries no side/ord_type at all
    (only order_id/client_order_id/created_at) — the shared parser must not guess."""
    from pycex.exchanges._krw_v1 import _parse_order

    raw_cancel_response = {
        "order_id": "order-uuid-1",
        "client_order_id": None,
        "created_at": "2026-08-30T00:00:00+09:00",
    }
    order = _parse_order("BTC/KRW", raw_cancel_response)
    assert order.side == "" and order.type == ""


async def test_fetch_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=_full_order_response())
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.fetch_order("order-uuid-1", "BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/order" and req.url.params["uuid"] == "order-uuid-1"
    _assert_bearer_query_hash(req.headers, {"uuid": "order-uuid-1"})
    assert order.symbol == "BTC/KRW"
    await ex.close()


async def test_fetch_open_orders(httpx_mock: HTTPXMock) -> None:
    pending_entry = {
        "order_id": "order-uuid-1",
        "side": "bid",
        "order_type": "limit",
        "price": "50000000.0",
        "state": "wait",
        "market": "KRW-BTC",
        "created_at": "2026-08-30T00:00:00+09:00",
        "volume": "0.01",
        "remaining_volume": "0.01",
        "executed_volume": "0.0",
    }
    httpx_mock.add_response(method="GET", json={"data": [pending_entry], "has_next": False, "next_key": None})
    ex = Bithumb(api_key="k", secret="s")
    orders = await ex.fetch_open_orders("BTC/KRW")
    req = httpx_mock.get_request()
    assert req.url.path == "/v2/orders/pending"
    assert req.url.params["state"] == "wait" and req.url.params["market"] == "KRW-BTC"
    expected_params = {"state": "wait", "market": "KRW-BTC"}
    _assert_bearer_query_hash(req.headers, expected_params)
    assert len(orders) == 1
    assert orders[0].symbol == "BTC/KRW"
    assert orders[0].id == "order-uuid-1"
    await ex.close()


async def test_fetch_my_trades_fans_out_per_order_lookup(httpx_mock: HTTPXMock) -> None:
    history_response = {
        "data": [
            {"order_id": "order-1", "side": "bid", "market": "KRW-BTC", "created_at": "2026-08-30T00:00:00+09:00"},
            {"order_id": "order-2", "side": "ask", "market": "KRW-BTC", "created_at": "2026-08-30T01:00:00+09:00"},
        ],
        "has_next": False,
        "next_key": None,
    }
    order_1_detail = {
        "uuid": "order-1",
        "market": "KRW-BTC",
        "trades": [
            {
                "market": "KRW-BTC",
                "uuid": "trade-1",
                "price": "100000000.0",
                "volume": "0.001",
                "created_at": "2026-08-30T00:00:00+09:00",
            }
        ],
    }
    order_2_detail = {
        "uuid": "order-2",
        "market": "KRW-BTC",
        "trades": [
            {
                "market": "KRW-BTC",
                "uuid": "trade-2",
                "price": "101000000.0",
                "volume": "0.002",
                "created_at": "2026-08-30T01:00:00+09:00",
            }
        ],
    }
    httpx_mock.add_response(method="GET", json=history_response)
    httpx_mock.add_response(method="GET", json=order_1_detail)
    httpx_mock.add_response(method="GET", json=order_2_detail)
    ex = Bithumb(api_key="k", secret="s")
    trades = await ex.fetch_my_trades("BTC/KRW")

    requests = httpx_mock.get_requests()
    assert len(requests) == 3  # 1 history call + N=2 per-order detail calls
    history_req, detail_req_1, detail_req_2 = requests
    assert history_req.url.path == "/v2/orders/history"
    assert history_req.url.params["state"] == "done" and history_req.url.params["market"] == "KRW-BTC"
    assert history_req.url.params["limit"] == "20"  # default N
    _assert_bearer_query_hash(history_req.headers, {"state": "done", "limit": 20, "market": "KRW-BTC"})
    assert detail_req_1.url.path == "/v1/order" and detail_req_1.url.params["uuid"] == "order-1"
    _assert_bearer_query_hash(detail_req_1.headers, {"uuid": "order-1"})
    assert detail_req_2.url.path == "/v1/order" and detail_req_2.url.params["uuid"] == "order-2"
    _assert_bearer_query_hash(detail_req_2.headers, {"uuid": "order-2"})

    assert len(trades) == 2
    assert trades[0].id == "trade-1" and trades[0].order_id == "order-1" and trades[0].fee_asset == "KRW"
    assert trades[1].id == "trade-2" and trades[1].order_id == "order-2" and trades[1].fee_asset == "KRW"
    await ex.close()


async def test_fetch_my_trades_limit_capped_at_50(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json={"data": [], "has_next": False, "next_key": None})
    ex = Bithumb(api_key="k", secret="s")
    await ex.fetch_my_trades("BTC/KRW", limit=500)
    req = httpx_mock.get_request()
    assert req.url.params["limit"] == "50"
    _assert_bearer_query_hash(req.headers, {"state": "done", "limit": 50, "market": "KRW-BTC"})
    await ex.close()


async def test_fetch_balance(httpx_mock: HTTPXMock) -> None:
    response = [
        {"currency": "KRW", "balance": "1000000.0", "locked": "0.0"},
        {"currency": "BTC", "balance": "0.5", "locked": "0.1"},
    ]
    httpx_mock.add_response(method="GET", json=response)
    ex = Bithumb(api_key="k", secret="s")
    balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/accounts"
    auth = req.headers.get("Authorization", "")
    assert auth.startswith("Bearer ")
    payload = _decode_jwt_payload(auth[len("Bearer ") :])
    assert "timestamp" in payload
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
        ("expired_jwt", AuthenticationError),
        ("NotAllowIP", AuthenticationError),
        ("order_not_found", OrderNotFoundError),
        ("some_unmapped_error", ExchangeError),
    ],
)
def test_map_error(name: str, expected_type: type[Exception]) -> None:
    exc = _map_error(400, {"error": {"name": name, "message": "boom"}})
    assert isinstance(exc, expected_type)


def test_map_error_int_name_is_not_a_crash() -> None:
    """Bithumb returns an **int** ``name`` for its unknown-market envelope (live probe
    2026-08-30: ``GET /v1/ticker?markets=KRW-NOPE`` -> HTTP **200**
    ``{"error":{"name":404,"message":"Code not found"}}``)."""
    exc = _map_error(200, {"error": {"name": 404, "message": "Code not found"}})
    assert isinstance(exc, SymbolNotFoundError)


def test_map_error_invalid_jwt_is_authentication_error() -> None:
    """Live probe 2026-08-30: ``GET /v1/accounts`` with a garbage bearer token ->
    HTTP 401 ``{"error":{"name":"invalid_jwt"}}``."""
    assert isinstance(_map_error(401, {"error": {"name": "invalid_jwt"}}), AuthenticationError)


async def test_fetch_ticker_unknown_market_raises_on_http_200(httpx_mock: HTTPXMock) -> None:
    """Bithumb serves its error envelope with HTTP 200, so the HTTPClient
    error_mapper (status >= 400 only) never sees it — the adapter must check."""
    httpx_mock.add_response(status_code=200, json={"error": {"name": 404, "message": "Code not found"}})
    ex = Bithumb()
    with pytest.raises(SymbolNotFoundError):
        await ex.fetch_ticker("NOPE/KRW")
    await ex.close()


async def test_candles_error_envelope_with_http_200_still_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=200, json={"error": {"name": 400, "message": "Invalid parameter."}})
    ex = Bithumb()
    with pytest.raises(ExchangeError):
        await ex._fetch_candles_page("KRW-BTC", "1d", since=None, until=None, limit=3)
    await ex.close()


async def test_fetch_balance_error_envelope_with_http_200_still_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=200, json={"error": {"name": "jwt_verification", "message": "bad"}})
    ex = Bithumb("k", "s")
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    await ex.close()


def test_format_to_is_naive_kst() -> None:
    """🚨 Bithumb rejects **any** timezone suffix on `to` and reads a naive value as
    KST (live probe 2026-08-30: ``to=2026-08-25T00:00:00Z`` and
    ``to=...+00:00`` both -> HTTP 200 ``{"error":{"name":400,...}}``;
    ``to=2026-08-25T00:00:00`` -> newest bar ``2026-08-23T15:00:00`` UTC, i.e. the
    value was read as 2026-08-25 00:00 KST = 2026-08-24T15:00Z, exclusive).
    Upbit reads the same field as UTC."""
    out = Bithumb()._format_to(1_787_616_000_000)  # 2026-08-25T00:00:00Z
    assert out == "2026-08-25T09:00:00"
    assert not out.endswith("Z") and "+" not in out


async def test_candles_page_to_param_is_naive_kst(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bithumb", "candles_1d"))
    ex = Bithumb()
    await ex._fetch_candles_page("KRW-BTC", "1d", since=None, until=1_787_615_999_999, limit=3)
    to = httpx_mock.get_request().url.params["to"]
    assert to == "2026-08-25T09:00:00"
    await ex.close()


# ── client_order_id: apidocs.bithumb.com 주문 요청 (POST /v2/orders) / 개별 주문 조회 (GET /v1/order) ──

CID = "qtx-0123456789abcdef0123456789abcdef"  # 36 chars of [A-Za-z0-9-]


async def test_create_order_sends_client_order_id_signed_and_echoes_it(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response(client_order_id=CID))
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=CID)
    req = httpx_mock.get_request()
    expected_body = {
        "market": "KRW-BTC",
        "side": "bid",
        "order_type": "limit",
        "volume": "0.01",
        "price": "50000000",
        "client_order_id": CID,
    }
    assert json.loads(req.content) == expected_body
    _assert_bearer_query_hash(req.headers, expected_body)  # client_order_id is inside the signed query_hash
    assert order.client_order_id == CID
    await ex.close()


async def test_create_order_client_order_id_echo_falls_back_to_request_value(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response())
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=CID)
    assert order.client_order_id == CID
    await ex.close()


async def test_create_order_without_client_order_id_sends_no_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response())
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000)
    assert "client_order_id" not in json.loads(httpx_mock.get_request().content)
    assert order.client_order_id is None
    await ex.close()


@pytest.mark.parametrize("bad", ["", "x" * 37, "has space", "a/b", "a+b", "한글", "a:b"])
async def test_create_order_rejects_invalid_client_order_id_without_request(httpx_mock: HTTPXMock, bad: str) -> None:
    ex = Bithumb(api_key="k", secret="s")
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=bad)
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("bad", ["", "x" * 37, "has space", "a/b", "a+b", "한글", "a:b", "a=b", "a.b"])
async def test_fetch_order_rejects_invalid_client_order_id_without_request(httpx_mock: HTTPXMock, bad: str) -> None:
    ex = Bithumb(api_key="k", secret="s")
    with pytest.raises(InvalidOrderError):
        await ex.fetch_order(None, "BTC/KRW", client_order_id=bad)
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("ok", ["x", "x" * 36, "A-b_9"])
async def test_fetch_order_accepts_the_same_range_as_create(httpx_mock: HTTPXMock, ok: str) -> None:
    httpx_mock.add_response(method="GET", json=_full_order_response(client_order_id=ok))
    ex = Bithumb(api_key="k", secret="s")
    await ex.fetch_order(None, "BTC/KRW", client_order_id=ok)
    assert dict(httpx_mock.get_request().url.params) == {"client_order_id": ok}
    await ex.close()


@pytest.mark.parametrize("ok", ["x", "x" * 36, "A-b_9"])
async def test_create_order_accepts_documented_client_order_id_range(httpx_mock: HTTPXMock, ok: str) -> None:
    httpx_mock.add_response(method="POST", json=_v2_create_response())
    ex = Bithumb(api_key="k", secret="s")
    await ex.create_order("BTC/KRW", "buy", "limit", 0.01, price=50_000_000, client_order_id=ok)
    assert json.loads(httpx_mock.get_request().content)["client_order_id"] == ok
    await ex.close()


async def test_fetch_order_by_client_order_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=_full_order_response(client_order_id=CID))
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CID)
    req = httpx_mock.get_request()
    assert req.url.path == "/v1/order"
    assert dict(req.url.params) == {"client_order_id": CID}  # no uuid alongside: precedence is undocumented
    _assert_bearer_query_hash(req.headers, {"client_order_id": CID})
    assert order.id == "order-uuid-1" and order.client_order_id == CID
    await ex.close()


async def test_fetch_order_by_uuid_still_sends_uuid_only(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=_full_order_response())
    ex = Bithumb(api_key="k", secret="s")
    order = await ex.fetch_order("order-uuid-1", "BTC/KRW")
    assert dict(httpx_mock.get_request().url.params) == {"uuid": "order-uuid-1"}
    assert order.client_order_id is None
    await ex.close()


@pytest.mark.parametrize(("order_id", "client_id"), [(None, None), ("", ""), ("order-uuid-1", CID)])
async def test_fetch_order_requires_exactly_one_key_without_request(
    httpx_mock: HTTPXMock, order_id: str | None, client_id: str | None
) -> None:
    ex = Bithumb(api_key="k", secret="s")
    with pytest.raises(InvalidOrderError):
        await ex.fetch_order(order_id, "BTC/KRW", client_order_id=client_id)
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_open_orders_and_cancel_echo_client_order_id(httpx_mock: HTTPXMock) -> None:
    pending = {
        "order_id": "order-uuid-1",
        "side": "bid",
        "order_type": "limit",
        "price": "50000000.0",
        "state": "wait",
        "market": "KRW-BTC",
        "created_at": "2026-08-30T00:00:00+09:00",
        "volume": "0.01",
        "remaining_volume": "0.01",
        "executed_volume": "0.0",
        "client_order_id": CID,
    }
    httpx_mock.add_response(method="GET", json={"data": [pending], "has_next": False, "next_key": None})
    httpx_mock.add_response(
        method="DELETE",
        json={"order_id": "order-uuid-1", "client_order_id": CID, "created_at": "2026-08-30T00:00:00+09:00"},
    )
    ex = Bithumb(api_key="k", secret="s")
    assert (await ex.fetch_open_orders("BTC/KRW"))[0].client_order_id == CID
    assert (await ex.cancel_order("order-uuid-1", "BTC/KRW")).client_order_id == CID
    await ex.close()

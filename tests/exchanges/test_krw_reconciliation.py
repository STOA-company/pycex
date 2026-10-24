"""Offline HTTP contracts for recovering a lost KRW order response.

These protect result identity, execution amounts, and cancellation acceptance
across all three adapters. They never send a second create after a timeout.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import NetworkError, OrderNotFoundError
from pycex.exchanges.bithumb import Bithumb
from pycex.exchanges.korbit import Korbit
from pycex.exchanges.upbit import Upbit

VENUES = [Upbit, Bithumb, Korbit]
CLIENT_ID = "w6-result-key"


def _detail(venue: type, **updates: Any) -> dict[str, Any]:
    if venue is Korbit:
        data = {
            "orderId": 1234,
            "clientOrderId": CLIENT_ID,
            "symbol": "btc_krw",
            "orderType": "limit",
            "side": "buy",
            "qty": "2",
            "price": "1200",
            "filledQty": "0.75",
            "filledAmt": "750",
            "avgPrice": "1000",
            "status": "partiallyFilled",
        }
    else:
        data = {
            "uuid": "1234",
            "identifier" if venue is Upbit else "client_order_id": CLIENT_ID,
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "limit",
            "volume": "2",
            "price": "1200",
            "executed_volume": "0.75",
            "remaining_volume": "1.25",
            "state": "wait",
            "trades": [{"volume": "0.25", "funds": "200"}, {"volume": "0.5", "funds": "550"}],
        }
        if venue is Bithumb:
            data["executed_funds"] = "750"
    data.update(updates)
    return {"success": True, "data": data} if venue is Korbit else data


@pytest.mark.parametrize("venue", VENUES)
async def test_timeout_then_lookup_by_same_client_key_keeps_actual_execution(
    venue: type, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("lost order response"), method="POST")
    httpx_mock.add_response(method="GET", json=_detail(venue))
    async with venue(api_key="test-key", secret="test-secret") as ex:
        with pytest.raises(NetworkError):
            await ex.create_order("BTC/KRW", "buy", "limit", 2, price=1200, client_order_id=CLIENT_ID)
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    create, lookup = httpx_mock.get_requests()
    assert [create.method, lookup.method] == ["POST", "GET"]
    if venue is Korbit:
        assert parse_qs(create.content.decode())["clientOrderId"] == [CLIENT_ID]
        assert lookup.url.params["symbol"] == "btc_krw"
        key = "clientOrderId"
        assert "orderId" not in lookup.url.params
    else:
        key = "identifier" if venue is Upbit else "client_order_id"
        assert json.loads(create.content)[key] == CLIENT_ID
        assert "uuid" not in lookup.url.params
    assert lookup.url.params[key] == CLIENT_ID
    assert order.id == "1234" and order.client_order_id == CLIENT_ID
    assert order.symbol == "BTC/KRW"
    assert order.amount == 2 and order.filled == 0.75
    assert order.average_price == 1000 and order.price == 1200


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("state", [None, "", "undocumented_state"])
async def test_unknown_result_is_never_terminal(venue: type, state: Any, httpx_mock: HTTPXMock) -> None:
    field = "status" if venue is Korbit else "state"
    httpx_mock.add_response(method="GET", json=_detail(venue, **{field: state}))
    async with venue(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.status == "unknown"
    assert order.raw[field] == state


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("native,expected", [("eth", "ETH/KRW"), ("invalid", ""), (None, "")])
async def test_lookup_preserves_response_market_identity(
    venue: type, native: str | None, expected: str, httpx_mock: HTTPXMock
) -> None:
    field = "symbol" if venue is Korbit else "market"
    value = None if native is None else native
    if native == "eth":
        value = "eth_krw" if venue is Korbit else "KRW-ETH"
    httpx_mock.add_response(method="GET", json=_detail(venue, **{field: value}))
    async with venue(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.symbol == expected


@pytest.mark.parametrize("venue", VENUES)
async def test_lookup_without_market_does_not_invent_requested_identity(venue: type, httpx_mock: HTTPXMock) -> None:
    payload = _detail(venue)
    data = payload["data"] if venue is Korbit else payload
    data.pop("symbol" if venue is Korbit else "market")
    httpx_mock.add_response(method="GET", json=payload)
    async with venue(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.symbol == ""


@pytest.mark.parametrize("venue", VENUES)
async def test_missing_lookup_retains_not_found_and_does_not_resubmit(venue: type, httpx_mock: HTTPXMock) -> None:
    payload = (
        {"success": False, "error": {"code": 404, "message": "ORDER_NOT_FOUND"}}
        if venue is Korbit
        else {"error": {"name": "order_not_found", "message": "order not found"}}
    )
    httpx_mock.add_response(method="GET", status_code=404, json=payload)
    async with venue(api_key="test-key", secret="test-secret") as ex:
        with pytest.raises(OrderNotFoundError):
            await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert [r.method for r in httpx_mock.get_requests()] == ["GET"]


@pytest.mark.parametrize("venue", [Bithumb, Korbit])
async def test_sparse_cancel_ack_requires_followup_and_keeps_filled_quantity(
    venue: type, httpx_mock: HTTPXMock
) -> None:
    ack = {"success": True} if venue is Korbit else {"order_id": "1234", "client_order_id": CLIENT_ID}
    field, state = ("status", "partiallyFilledCanceled") if venue is Korbit else ("state", "cancel")
    httpx_mock.add_response(method="DELETE", json=ack)
    httpx_mock.add_response(method="GET", json=_detail(venue, **{field: state}))
    async with venue(api_key="test-key", secret="test-secret") as ex:
        accepted = await ex.cancel_order("1234", "BTC/KRW")
        assert accepted.status == "unknown" and accepted.filled == 0
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.status == state and order.filled == 0.75 and order.average_price == 1000
    assert [r.method for r in httpx_mock.get_requests()] == ["DELETE", "GET"]


async def test_korbit_filled_status_can_have_less_than_requested_quantity(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json=_detail(Korbit, status="filled"))
    async with Korbit(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.status == "filled" and order.filled == 0.75 and order.amount == 2


@pytest.mark.parametrize("venue", [Upbit, Bithumb])
@pytest.mark.parametrize("trades", [[], [{"volume": "0.5", "funds": "500"}], [{"volume": "NaN", "funds": "750"}]])
async def test_incomplete_or_nonfinite_fills_never_use_limit_price(
    venue: type, trades: list, httpx_mock: HTTPXMock
) -> None:
    payload = _detail(venue, trades=trades)
    payload.pop("executed_funds", None)
    httpx_mock.add_response(method="GET", json=payload)
    async with venue(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.average_price is None and order.price == 1200


@pytest.mark.parametrize("average", [None, "0", "NaN", "Infinity", "bad"])
async def test_korbit_average_uses_only_valid_execution_fields(average: Any, httpx_mock: HTTPXMock) -> None:
    payload = _detail(Korbit, avgPrice=average)
    httpx_mock.add_response(method="GET", json=payload)
    async with Korbit(api_key="test-key", secret="test-secret") as ex:
        order = await ex.fetch_order(None, "BTC/KRW", client_order_id=CLIENT_ID)
    assert order.average_price == (1000 if average is None else None)

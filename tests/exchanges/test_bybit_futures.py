"""W6 Bybit linear contracts use V5 JSON/signatures, native statuses and explicit settings."""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import ExchangeError, InvalidOrderError, NetworkError, NotSupportedError, OrderNotFoundError
from pycex.exchanges.bybit import Bybit
from tests.exchanges.test_bybit import _assert_valid_get_signature, _assert_valid_post_signature

SYMBOL = "BTC/USDT:USDT"


async def test_linear_reduce_only_hedge_signed_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {"orderId": "7", "orderLinkId": "w6-close"}})
    async with Bybit(api_key="k", secret="s", market_type="linear", sandbox=True) as ex:
        order = await ex.create_order(
            SYMBOL, "sell", "limit", 0.02, 60000, reduce_only=True, position_idx=1, client_order_id="w6-close"
        )
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/v5/order/create" and "testnet" in req.url.host
    body = json.loads(req.content)
    assert body == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "orderType": "Limit",
        "qty": "0.02",
        "price": "60000",
        "timeInForce": "GTC",
        "orderLinkId": "w6-close",
        "reduceOnly": True,
        "positionIdx": 1,
    }
    _assert_valid_post_signature(req)
    assert order.id == "7" and order.client_order_id == "w6-close" and order.status == "" and order.filled == 0


@pytest.mark.parametrize("kwargs", [{"position_idx": 0}, {"position_idx": 2}, {}])
async def test_optional_linear_order_fields(httpx_mock: HTTPXMock, kwargs: dict) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {"orderId": "7"}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        await ex.create_order(SYMBOL, "buy", "market", 0.01, **kwargs)
    req = httpx_mock.get_request()
    assert req is not None
    body = json.loads(req.content)
    assert "reduceOnly" not in body
    assert body.get("positionIdx") == kwargs.get("position_idx")
    assert "price" not in body and "timeInForce" not in body


@pytest.mark.parametrize("kwargs", [{"reduce_only": True}, {"position_idx": 0}])
async def test_spot_futures_options_rejected(httpx_mock: HTTPXMock, kwargs: dict) -> None:
    async with Bybit() as ex:
        with pytest.raises(NotSupportedError):
            await ex.create_order("BTC/USDT", "sell", "market", 1, **kwargs)
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("kwargs", [{"reduce_only": "false"}, {"position_idx": True}, {"position_idx": 3}])
async def test_invalid_futures_options_rejected(httpx_mock: HTTPXMock, kwargs: dict) -> None:
    async with Bybit(market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order(SYMBOL, "sell", "market", 1, **kwargs)
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("ret_code", [0, 110043])
async def test_leverage_ack_preserves_success_or_unchanged(httpx_mock: HTTPXMock, ret_code: int) -> None:
    raw = {
        "retCode": ret_code,
        "retMsg": "OK" if ret_code == 0 else "Set leverage has not been modified.",
        "result": {},
    }
    httpx_mock.add_response(json=raw)
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.set_leverage(SYMBOL, 3.0) == raw
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/v5/position/set-leverage"
    assert json.loads(req.content) == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "buyLeverage": "3",
        "sellLeverage": "3",
    }
    _assert_valid_post_signature(req)
    assert len(httpx_mock.get_requests()) == 1  # no implicit account-wide switch


@pytest.mark.parametrize("status", [200, 400])
async def test_leverage_rejection_propagates(httpx_mock: HTTPXMock, status: int) -> None:
    httpx_mock.add_response(status_code=status, json={"retCode": 110013, "retMsg": "risk limit"})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError) as err:
            await ex.set_leverage(SYMBOL, 3)
    assert str(err.value.code) == "110013"
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("lever", [0, -1, float("nan"), float("inf"), True])
async def test_invalid_leverage_has_no_http(httpx_mock: HTTPXMock, lever: float) -> None:
    async with Bybit(market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.set_leverage(SYMBOL, lever)
    assert httpx_mock.get_requests() == []


async def test_mode_argument_does_not_switch_account(httpx_mock: HTTPXMock) -> None:
    async with Bybit(market_type="linear") as ex:
        with pytest.raises(NotSupportedError, match="account-wide"):
            await ex.set_leverage(SYMBOL, 3, "isolated")
    async with Bybit() as ex:
        with pytest.raises(NotSupportedError):
            await ex.set_leverage("BTC/USDT", 3)
        with pytest.raises(NotSupportedError):
            await ex.set_margin_mode("isolated")
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("side,kind", [("typo", "market"), ("buy", "limti")])
async def test_order_typo_does_not_turn_into_sell_or_market(httpx_mock: HTTPXMock, side: str, kind: str) -> None:
    async with Bybit(market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order(SYMBOL, side, kind, 0.01)
    assert httpx_mock.get_requests() == []


async def test_fractional_leverage_not_rounded(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        await ex.set_leverage(SYMBOL, 1.234567890123456)
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.content)["buyLeverage"] == "1.234567890123456"


async def test_http_400_unchanged_leverage_keeps_original_code(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"retCode": 110043, "retMsg": "Set leverage has not been modified."})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        ack = await ex.set_leverage(SYMBOL, 3)
    assert ack["retCode"] == 110043 and "leverage" not in ack


@pytest.mark.parametrize(
    "mode,native", [("cross", "REGULAR_MARGIN"), ("isolated", "ISOLATED_MARGIN"), ("portfolio", "PORTFOLIO_MARGIN")]
)
async def test_explicit_account_wide_margin_mode(httpx_mock: HTTPXMock, mode: str, native: str) -> None:
    raw = {"retCode": 0, "result": {"reasons": []}}
    httpx_mock.add_response(json=raw)
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.set_margin_mode(mode) == raw
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/v5/account/set-margin-mode"
    assert json.loads(req.content) == {"setMarginMode": native}
    _assert_valid_post_signature(req)


@pytest.mark.parametrize(
    "raw",
    [
        {"retCode": 3400045, "result": {"reasons": [{"reasonCode": "3400000"}]}},
        {"retCode": 0, "result": {}},
        {"retCode": 0, "result": {"reasons": ["failed"]}},
    ],
)
async def test_margin_reasons_or_missing_confirmation_are_not_success(httpx_mock: HTTPXMock, raw: dict) -> None:
    httpx_mock.add_response(json=raw)
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.set_margin_mode("isolated")


async def test_fetch_positions_pagination_and_direction(httpx_mock: HTTPXMock) -> None:
    short = {
        "symbol": "BTCUSDT",
        "side": "Sell",
        "size": "0.01",
        "avgPrice": "60000",
        "unrealisedPnl": "-2",
        "leverage": "3",
        "liqPrice": "",
        "updatedTime": "123",
        "tradeMode": 0,
    }
    long = {"symbol": "ETHUSDT", "side": "Buy", "size": "1", "leverage": "", "liqPrice": "0"}
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": [short], "nextPageCursor": "p2"}})
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": [long, {"size": "0"}], "nextPageCursor": ""}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        positions = await ex.fetch_positions()
    requests = httpx_mock.get_requests()
    assert requests[0].url.params["settleCoin"] == "USDT" and requests[0].url.params["limit"] == "200"
    assert requests[1].url.params["cursor"] == "p2"
    for req in requests:
        assert req.url.path == "/v5/position/list"
        _assert_valid_get_signature(req)
    assert positions[0].amount == 0.01 and positions[0].signed_amount == -0.01 and positions[0].side == "short"
    assert positions[0].entry_price == 60000 and positions[0].unrealized_pnl == -2
    assert positions[0].margin_mode is None and positions[0].liquidation_price is None
    assert positions[0].raw == short and positions[1].symbol == "ETH/USDT:USDT" and positions[1].leverage is None


async def test_fetch_positions_explicit_symbols_and_empty_list(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": []}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.fetch_positions([SYMBOL, SYMBOL]) == []
        assert await ex.fetch_positions([]) == []
    req = httpx_mock.get_request()
    assert req is not None and req.url.params["symbol"] == "BTCUSDT" and "settleCoin" not in req.url.params


async def test_unknown_position_direction_is_not_guessed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "1"}]}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError, match="direction"):
            await ex.fetch_positions()


async def test_isolated_leverage_snapshot_without_open_position(httpx_mock: HTTPXMock) -> None:
    rows = [{"symbol": "BTCUSDT", "leverage": "3", "positionIdx": 0}]
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": rows}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.fetch_leverage(SYMBOL) == rows
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/v5/position/symbol-info" and req.url.params["symbol"] == "BTCUSDT"
    _assert_valid_get_signature(req)


async def test_uta_margin_snapshot_uses_account_not_deprecated_position_trade_mode(httpx_mock: HTTPXMock) -> None:
    row = {"marginMode": "ISOLATED_MARGIN", "unifiedMarginStatus": 5}
    httpx_mock.add_response(json={"retCode": 0, "result": row})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.fetch_account_config() == row
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/v5/account/info"
    _assert_valid_get_signature(req)


async def test_linear_open_orders_settlement_pagination(httpx_mock: HTTPXMock) -> None:
    row = {"symbol": "BTCUSDT", "orderId": "7", "orderStatus": "PartiallyFilled", "qty": "0.02", "cumExecQty": "0.01"}
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": [row], "nextPageCursor": "p2"}})
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": [], "nextPageCursor": ""}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        orders = await ex.fetch_open_orders()
    requests = httpx_mock.get_requests()
    assert requests[0].url.params["settleCoin"] == "USDT" and requests[1].url.params["cursor"] == "p2"
    assert orders[0].status == "PartiallyFilled" and orders[0].filled == 0.01


async def test_private_cursor_cycle_is_not_partial_success(httpx_mock: HTTPXMock) -> None:
    for _ in range(2):
        httpx_mock.add_response(json={"retCode": 0, "result": {"list": [], "nextPageCursor": "repeat"}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError, match="cursor repeated"):
            await ex.fetch_open_orders()


async def test_cancel_ack_then_query_terminal_status(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {"orderId": "7"}})
    httpx_mock.add_response(
        json={"retCode": 0, "result": {"list": [{"orderId": "7", "symbol": "BTCUSDT", "orderStatus": "Cancelled"}]}}
    )
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        ack = await ex.cancel_order("7", SYMBOL)
        assert ack.status == "" and ack.filled == 0
        order = await ex.fetch_order("7", SYMBOL)
        assert order.status == "Cancelled"
    requests = httpx_mock.get_requests()
    assert json.loads(requests[0].content) == {"category": "linear", "symbol": "BTCUSDT", "orderId": "7"}
    _assert_valid_post_signature(requests[0])
    _assert_valid_get_signature(requests[1])


async def test_lost_create_response_reconciles_same_client_id_in_history(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("synthetic lost response"))
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": []}})
    httpx_mock.add_response(
        json={
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "orderId": "7",
                        "symbol": "BTCUSDT",
                        "orderLinkId": "w6-close",
                        "orderStatus": "New",
                        "qty": "0.01",
                    }
                ]
            },
        }
    )
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(NetworkError):
            await ex.create_order(SYMBOL, "sell", "market", 0.01, reduce_only=True, client_order_id="w6-close")
        order = await ex.fetch_order(None, SYMBOL, client_order_id="w6-close")
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    for req in requests[1:]:
        assert req.url.params["category"] == "linear" and req.url.params["orderLinkId"] == "w6-close"
        _assert_valid_get_signature(req)
    assert order.id == "7" and order.status == "New" and order.filled == 0


@pytest.mark.parametrize("action", ["create", "fetch", "cancel", "leverage"])
@pytest.mark.parametrize("raw", [{}, {"retCode": 0}, {"retCode": 0, "result": None}])
async def test_missing_envelope_is_not_success(httpx_mock: HTTPXMock, action: str, raw: dict) -> None:
    httpx_mock.add_response(json=raw)
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            if action == "create":
                await ex.create_order(SYMBOL, "sell", "market", 0.01, reduce_only=True)
            elif action == "fetch":
                await ex.fetch_order("7", SYMBOL)
            elif action == "cancel":
                await ex.cancel_order("7", SYMBOL)
            else:
                await ex.set_leverage(SYMBOL, 3)
    assert len(httpx_mock.get_requests()) == 1


async def test_missing_lookup_list_is_unknown_not_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError) as err:
            await ex.fetch_order(None, SYMBOL, client_order_id="w6-close")
        assert not isinstance(err.value, OrderNotFoundError)
    assert len(httpx_mock.get_requests()) == 1


async def test_empty_cancel_ack_stays_unknown(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        order = await ex.cancel_order("7", SYMBOL)
    assert order.id == "" and order.status == "" and order.filled == 0


def test_sync_futures_order_leverage_and_positions(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 0, "result": {}})
    httpx_mock.add_response(json={"retCode": 0, "result": {"orderId": "7"}})
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": []}})
    ex = Bybit(api_key="k", secret="s", market_type="linear")
    assert ex.set_leverage_sync(SYMBOL, 3)["retCode"] == 0
    assert ex.create_order_sync(SYMBOL, "sell", "market", 0.01, reduce_only=True).status == ""
    assert ex.fetch_positions_sync() == []


@pytest.mark.parametrize(
    "lookup,row",
    [
        ({"order_id": "7"}, {"orderId": "99", "symbol": "BTCUSDT"}),
        (
            {"order_id": None, "client_order_id": "w6-close"},
            {"orderId": "7", "orderLinkId": "foreign", "symbol": "BTCUSDT"},
        ),
        ({"order_id": "7"}, {"orderId": "7", "symbol": "ETHUSDT"}),
        ({"order_id": None, "client_order_id": "w6-close"}, {"orderId": "7", "symbol": "BTCUSDT"}),
        ({"order_id": "7"}, {"orderId": "7"}),
    ],
)
async def test_lookup_rejects_foreign_or_unconfirmed_identity(httpx_mock: HTTPXMock, lookup: dict, row: dict) -> None:
    httpx_mock.add_response(
        json={"retCode": 0, "result": {"list": [{**row, "orderStatus": "Filled", "cumExecQty": "1"}]}}
    )
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError, match="lookup"):
            await ex.fetch_order(symbol=SYMBOL, **lookup)
    assert len(httpx_mock.get_requests()) == 1  # a malformed hit is not a confirmed miss


async def test_lookup_selects_requested_identity_instead_of_first_row(httpx_mock: HTTPXMock) -> None:
    rows = [
        {"orderId": "foreign", "orderStatus": "Filled"},
        {"orderId": "7", "symbol": "BTCUSDT", "orderStatus": "New"},
    ]
    httpx_mock.add_response(json={"retCode": 0, "result": {"list": rows}})
    async with Bybit(api_key="k", secret="s", market_type="linear") as ex:
        order = await ex.fetch_order("7", SYMBOL)
    assert order.id == "7" and order.status == "New" and order.filled == 0

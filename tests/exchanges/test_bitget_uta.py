"""Official v3 private contracts and classic isolated-futures regressions.

All identifiers and balances are synthetic. Tests use the suite socket guard.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import ExchangeError, InvalidOrderError, NetworkError, NotSupportedError
from pycex.exchanges.bitget import Bitget
from tests.exchanges.test_bitget import _assert_valid_signature

SYMBOL = "BTC/USDT:USDT"


def success(data: object) -> dict:
    return {"code": "00000", "msg": "success", "data": data}


def order_row(**changes: object) -> dict:
    return {
        "orderId": "order-fixture",
        "clientOid": "w6-fixture",
        "category": "USDT-FUTURES",
        "symbol": "BTCUSDT",
        "orderType": "limit",
        "side": "sell",
        "qty": "0.2",
        "price": "60000",
        "cumExecQty": "0.05",
        "avgPrice": "59999",
        "orderStatus": "partially_filled",
        "createdTime": "1700000000000",
        **changes,
    }


@pytest.mark.parametrize(
    "market_type, symbol, category",
    [
        ("spot", "BTC/USDT", "SPOT"),
        ("linear", SYMBOL, "USDT-FUTURES"),
    ],
)
async def test_uta_limit_place_uses_v3_contract_and_exact_signature(
    httpx_mock: HTTPXMock,
    market_type: str,
    symbol: str,
    category: str,
) -> None:
    httpx_mock.add_response(json=success({"orderId": "order-fixture", "clientOid": "w6-fixture"}))
    async with Bitget(api_key="k", secret="s", passphrase="p", account_type="uta", market_type=market_type) as ex:
        order = await ex.create_order(symbol, "buy", "limit", 0.2, 60000, client_order_id="w6-fixture")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/trade/place-order"
    expected = {
        "category": category,
        "symbol": "BTCUSDT",
        "qty": "0.2",
        "price": "60000",
        "side": "buy",
        "orderType": "limit",
        "timeInForce": "gtc",
        "clientOid": "w6-fixture",
    }
    if market_type == "linear":
        expected["marginMode"] = "crossed"
    assert json.loads(req.content) == expected
    _assert_valid_signature(req)
    assert order.id == "order-fixture" and order.client_order_id == "w6-fixture"
    assert order.status == ""  # ACK does not establish fill/matching success.


@pytest.mark.parametrize("side,amount", [("buy", 100.0), ("sell", 0.2)])
async def test_uta_spot_market_qty_retains_exchange_quote_buy_base_sell_units(
    httpx_mock: HTTPXMock,
    side: str,
    amount: float,
) -> None:
    httpx_mock.add_response(json=success({"orderId": "order-fixture"}))
    async with Bitget(account_type="uta") as ex:
        await ex.create_order("BTC/USDT", side, "market", amount)
    body = json.loads(httpx_mock.get_request().content)
    assert body["qty"] == str(amount)
    assert "price" not in body and "timeInForce" not in body


@pytest.mark.parametrize("account_type, reduce_value", [("classic", "YES"), ("uta", "yes")])
async def test_isolated_reduce_order_never_switches_account_implicitly(
    httpx_mock: HTTPXMock,
    account_type: str,
    reduce_value: str,
) -> None:
    httpx_mock.add_response(json=success({"orderId": "order-fixture"}))
    async with Bitget(account_type=account_type, market_type="linear", secret="s") as ex:
        await ex.create_order(
            SYMBOL, "sell", "limit", 0.2, 60000, margin_mode="isolated", reduce_only=True, time_in_force="ioc"
        )
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    assert body["marginMode"] == "isolated"
    assert body["reduceOnly"] == reduce_value
    assert body["force" if account_type == "classic" else "timeInForce"] == "ioc"
    assert len(httpx_mock.get_requests()) == 1
    _assert_valid_signature(req)


@pytest.mark.parametrize("market_type,symbol", [("spot", "BTC/USDT"), ("linear", SYMBOL)])
async def test_uta_demo_uses_header_and_regular_native_symbols(
    httpx_mock: HTTPXMock,
    market_type: str,
    symbol: str,
) -> None:
    httpx_mock.add_response(json=success({"orderId": "order-fixture"}))
    async with Bitget(account_type="uta", market_type=market_type, sandbox=True) as ex:
        assert ex.to_native(symbol) == "BTCUSDT"
        assert ex.from_native("BTCUSDT") == symbol
        await ex.create_order(symbol, "sell", "market", 0.2)
    req = httpx_mock.get_request()
    assert req.headers["paptrading"] == "1"
    assert json.loads(req.content)["category"] in ("SPOT", "USDT-FUTURES")


@pytest.mark.parametrize("by_client", [False, True])
async def test_uta_order_lookup_parses_partial_fill_and_signs_exact_query(
    httpx_mock: HTTPXMock,
    by_client: bool,
) -> None:
    httpx_mock.add_response(json=success(order_row()))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        order = await ex.fetch_order(
            None if by_client else "order-fixture", SYMBOL, client_order_id="w6-fixture" if by_client else None
        )
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/trade/order-info"
    assert dict(req.url.params) == ({"clientOid": "w6-fixture"} if by_client else {"orderId": "order-fixture"})
    _assert_valid_signature(req)
    assert order.amount == 0.2 and order.filled == 0.05 and order.price == 60000
    assert order.status == "partially_filled" and order.timestamp == 1700000000000
    assert order.client_order_id == "w6-fixture"


async def test_uta_cancel_ack_does_not_fabricate_terminal_status(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=success({"orderId": "order-fixture", "clientOid": "w6-fixture"}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        order = await ex.cancel_order("order-fixture", SYMBOL)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/trade/cancel-order"
    assert json.loads(req.content) == {"category": "USDT-FUTURES", "orderId": "order-fixture"}
    assert order.status == "" and order.filled == 0
    _assert_valid_signature(req)


async def test_uta_open_orders_uses_list_wrapper(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=success({"list": [order_row()], "cursor": ""}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        orders = await ex.fetch_open_orders(SYMBOL)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/trade/unfilled-orders"
    assert dict(req.url.params) == {"category": "USDT-FUTURES", "symbol": "BTCUSDT", "limit": "100"}
    assert len(orders) == 1 and orders[0].filled == 0.05
    _assert_valid_signature(req)


@pytest.mark.parametrize("market_type", ["spot", "linear"])
async def test_uta_balance_uses_common_assets_and_keeps_locked(httpx_mock: HTTPXMock, market_type: str) -> None:
    httpx_mock.add_response(json=success({"assets": [{"coin": "USDT", "available": "20", "locked": "3"}]}))
    async with Bitget(account_type="uta", market_type=market_type, secret="s") as ex:
        balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/account/assets" and not req.url.params
    assert balance.get("USDT").free == 20 and balance.get("USDT").locked == 3
    _assert_valid_signature(req)


async def test_uta_positions_uses_v3_fields_and_filters_requested_symbols(httpx_mock: HTTPXMock) -> None:
    row = {
        "symbol": "BTCUSDT",
        "posSide": "short",
        "total": "0.2",
        "avgPrice": "60000",
        "unrealisedPnl": "-3.2",
        "leverage": "4",
        "liquidationPrice": "75000",
        "createdTime": "1234",
    }
    httpx_mock.add_response(json=success({"list": [row, {**row, "symbol": "ETHUSDT"}, {**row, "total": "0"}]}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        positions = await ex.fetch_positions([SYMBOL])
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/position/current-position"
    assert dict(req.url.params) == {"category": "USDT-FUTURES"}
    assert len(positions) == 1
    p = positions[0]
    assert (p.side, p.amount, p.entry_price, p.unrealized_pnl, p.leverage, p.timestamp) == (
        "short",
        0.2,
        60000,
        -3.2,
        4,
        1234,
    )
    _assert_valid_signature(req)


async def test_uta_own_fills_map_exec_fields_and_locally_filter_symbol(httpx_mock: HTTPXMock) -> None:
    row = {
        "execId": "fill-fixture",
        "orderId": "order-fixture",
        "symbol": "BTCUSDT",
        "side": "sell",
        "execPrice": "59999",
        "execQty": "0.05",
        "feeDetail": [{"fee": "0.3", "feeCoin": "USDT"}],
        "createdTime": "1234",
    }
    httpx_mock.add_response(json=success({"list": [row, {**row, "symbol": "ETHUSDT"}], "cursor": ""}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        fills = await ex.fetch_my_trades(SYMBOL, since=1000, limit=50)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/trade/fills"
    assert dict(req.url.params) == {"category": "USDT-FUTURES", "startTime": "1000", "limit": "50"}
    assert len(fills) == 1
    fill = fills[0]
    assert (fill.id, fill.amount, fill.price, fill.fee, fill.fee_asset) == ("fill-fixture", 0.05, 59999, 0.3, "USDT")
    _assert_valid_signature(req)


@pytest.mark.parametrize("mode", ["unified", "hybrid"])
async def test_read_only_detection_returns_uta_without_switching_adapter(httpx_mock: HTTPXMock, mode: str) -> None:
    httpx_mock.add_response(json=success({"accountMode": mode, "holdMode": "one_way_mode"}))
    async with Bitget(secret="s") as ex:
        assert await ex.detect_account_type() == "uta"
        assert ex.account_type == "classic"
    req = httpx_mock.get_request()
    assert req.method == "GET" and req.url.path == "/api/v3/account/settings"
    _assert_valid_signature(req)


@pytest.mark.parametrize(
    "settings", [{}, {"accountMode": "upgrading"}, {"accountMode": "switching"}, {"accountMode": "other"}]
)
async def test_detection_never_assumes_classic_for_unknown_or_transitional_modes(
    httpx_mock: HTTPXMock,
    settings: dict,
) -> None:
    httpx_mock.add_response(json=success(settings))
    async with Bitget() as ex:
        with pytest.raises(ExchangeError):
            await ex.detect_account_type()


@pytest.mark.parametrize("account_type", ["classic", "uta"])
@pytest.mark.parametrize("code", ["40085", "40725", "45001"])
async def test_order_errors_keep_code_and_never_replay_post(
    httpx_mock: HTTPXMock,
    code: str,
    account_type: str,
) -> None:
    httpx_mock.add_response(json={"code": code, "msg": "synthetic rejection", "data": None})
    kwargs = {"account_type": "uta"} if account_type == "uta" else {}
    async with Bitget(market_type="linear", **kwargs) as ex:
        with pytest.raises(ExchangeError) as error:
            await ex.create_order(SYMBOL, "sell", "market", 0.2, client_order_id="w6-fixture")
    assert str(error.value.code) == code
    assert len(httpx_mock.get_requests()) == 1


async def test_timed_out_uta_order_can_be_queried_by_same_client_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("synthetic lost order response"))
    httpx_mock.add_response(json=success(order_row(orderStatus="filled", cumExecQty="0.2")))
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(NetworkError):
            await ex.create_order(SYMBOL, "sell", "limit", 0.2, 60000, client_order_id="w6-fixture")
        order = await ex.fetch_order(None, SYMBOL, client_order_id="w6-fixture")
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["POST", "GET"]
    assert json.loads(requests[0].content)["clientOid"] == requests[1].url.params["clientOid"]
    assert order.status == "filled" and order.filled == 0.2


@pytest.mark.parametrize("payload", [{}, {"data": {}}, success(None)])
async def test_missing_uta_response_is_not_an_empty_success(httpx_mock: HTTPXMock, payload: dict) -> None:
    httpx_mock.add_response(json=payload)
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.fetch_order("order-fixture", SYMBOL)


async def test_classic_isolated_configuration_is_explicit_then_leverage_then_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=success({"marginMode": "isolated"}))
    httpx_mock.add_response(json=success({"longLeverage": "3", "shortLeverage": "3"}))
    httpx_mock.add_response(json=success({"orderId": "order-fixture"}))
    async with Bitget(market_type="linear", secret="s") as ex:
        await ex.set_margin_mode(SYMBOL, "isolated")
        await ex.set_leverage(SYMBOL, 3, "isolated")
        await ex.create_order(SYMBOL, "sell", "market", 0.2, margin_mode="isolated")
    requests = httpx_mock.get_requests()
    assert [r.url.path for r in requests] == [
        "/api/v2/mix/account/set-margin-mode",
        "/api/v2/mix/account/set-leverage",
        "/api/v2/mix/order/place-order",
    ]
    assert json.loads(requests[0].content) == {
        "productType": "USDT-FUTURES",
        "symbol": "BTCUSDT",
        "marginCoin": "USDT",
        "marginMode": "isolated",
    }
    assert json.loads(requests[1].content)["leverage"] == "3"
    for request in requests:
        _assert_valid_signature(request)


@pytest.mark.parametrize("mode,side", [("cross", None), ("isolated", "short")])
async def test_uta_leverage_contract(httpx_mock: HTTPXMock, mode: str, side: str | None) -> None:
    httpx_mock.add_response(json=success("success"))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        result = await ex.set_leverage(SYMBOL, 3, mode, pos_side=side)
    req = httpx_mock.get_request()
    expected = {
        "category": "USDT-FUTURES",
        "symbol": "BTCUSDT",
        "leverage": "3",
        "marginMode": "isolated" if mode == "isolated" else "crossed",
    }
    if side:
        expected["posSide"] = side
    assert req.url.path == "/api/v3/account/set-leverage"
    assert json.loads(req.content) == expected
    assert result["data"] == "success"
    _assert_valid_signature(req)


@pytest.mark.parametrize("lever", [0, -1, float("nan"), float("inf")])
async def test_invalid_leverage_is_rejected_before_request(lever: float) -> None:
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.set_leverage(SYMBOL, lever)


async def test_isolated_uta_leverage_requires_direction_before_request() -> None:
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.set_leverage(SYMBOL, 3, "isolated")


async def test_spot_futures_options_and_invalid_margin_mode_fail_before_request() -> None:
    async with Bitget(account_type="uta") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order("BTC/USDT", "buy", "market", 100, margin_mode="isolated")
        with pytest.raises(NotSupportedError):
            await ex.set_leverage("BTC/USDT", 3)
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order(SYMBOL, "buy", "market", 0.2, margin_mode="invalid")


@pytest.mark.parametrize(
    "changes",
    [{"orderId": "other"}, {"clientOid": "other"}, {"category": "SPOT"}, {"symbol": "ETHUSDT"}, {"orderId": ""}],
)
async def test_uta_order_lookup_never_accepts_mismatched_identity(httpx_mock: HTTPXMock, changes: dict) -> None:
    httpx_mock.add_response(json=success(order_row(**changes)))
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            if "orderId" in changes:
                await ex.fetch_order("order-fixture", SYMBOL)
            else:
                await ex.fetch_order(None, SYMBOL, client_order_id="w6-fixture")


@pytest.mark.parametrize("payload", [{}, {"code": "00000"}, success(None)])
async def test_leverage_requires_complete_ack(httpx_mock: HTTPXMock, payload: dict) -> None:
    httpx_mock.add_response(json=payload)
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.set_leverage(SYMBOL, 3)


@pytest.mark.parametrize("row", [{"symbol": "BTCUSDT"}, {"total": "NaN"}, {"total": "Infinity"}])
async def test_uta_missing_or_nonfinite_position_quantity_is_never_no_exposure(
    httpx_mock: HTTPXMock,
    row: dict,
) -> None:
    httpx_mock.add_response(json=success({"list": [row]}))
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.fetch_positions()


@pytest.mark.parametrize("payload", [success({}), success({"list": None}), success({"list": [None]})])
async def test_uta_missing_positions_list_does_not_mean_empty_account(httpx_mock: HTTPXMock, payload: dict) -> None:
    httpx_mock.add_response(json=payload)
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.fetch_positions()


@pytest.mark.parametrize("changes", [{"qty": "NaN"}, {"qty": None}, {"cumExecQty": "Infinity"}])
async def test_uta_nonfinite_order_quantity_is_not_accepted(httpx_mock: HTTPXMock, changes: dict) -> None:
    httpx_mock.add_response(json=success(order_row(**changes)))
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError):
            await ex.fetch_order("order-fixture", SYMBOL)


@pytest.mark.parametrize(
    "amount,price",
    [(float("nan"), 60000), (float("inf"), 60000), (-1, 60000), (0.2, float("nan")), (0.2, 0), (0.2, None)],
)
async def test_invalid_order_numbers_reject_before_write(amount: float, price: float | None) -> None:
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.create_order(SYMBOL, "sell", "limit", amount, price)


@pytest.mark.parametrize("echo_key", [True, False])
async def test_uta_null_order_ack_keeps_key_for_lookup_without_replay(httpx_mock: HTTPXMock, echo_key: bool) -> None:
    ack = {"orderId": None, **({"clientOid": "w6-fixture"} if echo_key else {})}
    httpx_mock.add_response(json=success(ack))
    httpx_mock.add_response(json=success(order_row()))
    async with Bitget(account_type="uta", market_type="linear") as ex:
        order = await ex.create_order(SYMBOL, "sell", "market", 0.2, reduce_only=True, client_order_id="w6-fixture")
        assert order.id == "" and order.client_order_id == "w6-fixture"
        assert order.status == "" and order.filled == 0
        assert order.raw == success(ack)
        reconciled = await ex.fetch_order(None, SYMBOL, client_order_id=order.client_order_id)
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["POST", "GET"]
    assert requests[1].url.params["clientOid"] == "w6-fixture"
    assert reconciled.id == "order-fixture" and reconciled.status == "partially_filled"


@pytest.mark.parametrize(
    "category,side,order_type,expected",
    [
        ("SPOT", "buy", "market", 100),
        ("SPOT", "sell", "market", 0.2),
        ("SPOT", "buy", "limit", 0.2),
        ("USDT-FUTURES", "buy", "market", 0.2),
    ],
)
async def test_uta_lookup_preserves_quote_market_buy_and_base_other_units(
    httpx_mock: HTTPXMock, category: str, side: str, order_type: str, expected: float
) -> None:
    row = order_row(
        category=category,
        side=side,
        orderType=order_type,
        qty="0" if expected == 100 else "0.2",
        amount="100",
        cumExecQty="0.0372",
    )
    httpx_mock.add_response(json=success(row))
    symbol = "BTC/USDT" if category == "SPOT" else SYMBOL
    async with Bitget(account_type="uta", market_type="spot" if category == "SPOT" else "linear") as ex:
        order = await ex.fetch_order("order-fixture", symbol)
    assert order.amount == expected and order.filled == 0.0372


async def test_uta_open_orders_exhausts_cursor_and_signs_each_page(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=success({"list": [order_row()], "cursor": "page-two"}))
    httpx_mock.add_response(json=success({"list": [order_row(orderId="second-order")], "cursor": ""}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        orders = await ex.fetch_open_orders(SYMBOL)
    assert [o.id for o in orders] == ["order-fixture", "second-order"]
    first, second = httpx_mock.get_requests()
    assert dict(first.url.params) == {"category": "USDT-FUTURES", "symbol": "BTCUSDT", "limit": "100"}
    assert dict(second.url.params) == {**dict(first.url.params), "cursor": "page-two"}
    for request in (first, second):
        _assert_valid_signature(request)


def fill_row(**changes: object) -> dict:
    return {
        "execId": "fill-fixture",
        "orderId": "order-fixture",
        "symbol": "BTCUSDT",
        "side": "sell",
        "execPrice": "59999",
        "execQty": "0.05",
        "createdTime": "1234",
        **changes,
    }


@pytest.mark.parametrize("limit", [None, 1, 101])
async def test_uta_fill_pagination_filters_before_matching_output_limit(
    httpx_mock: HTTPXMock, limit: int | None
) -> None:
    httpx_mock.add_response(json=success({"list": [fill_row(symbol="ETHUSDT")], "cursor": "page-two"}))
    count = 2 if limit is None or limit == 1 else 100
    httpx_mock.add_response(
        json=success({"list": [fill_row(execId=f"match-{i}") for i in range(count)], "cursor": "page-three"})
    )
    if limit != 1:
        httpx_mock.add_response(json=success({"list": [fill_row(execId="last-fill")], "cursor": ""}))
    async with Bitget(account_type="uta", market_type="linear", secret="s") as ex:
        fills = await ex.fetch_my_trades(SYMBOL, since=1000, limit=limit)
    assert len(fills) == (3 if limit is None else limit)
    assert len({f.id for f in fills}) == len(fills)  # Distinct fills of the same order survive.
    requests = httpx_mock.get_requests()
    assert len(requests) == (2 if limit == 1 else 3)
    for i, request in enumerate(requests):
        assert request.url.params["category"] == "USDT-FUTURES"
        assert request.url.params["startTime"] == "1000"
        assert request.url.params["limit"] == str(min(limit, 100) if limit is not None else 100)
        assert "symbol" not in request.url.params
        assert request.url.params.get("cursor") == (None if i == 0 else ("page-two" if i == 1 else "page-three"))
        _assert_valid_signature(request)


@pytest.mark.parametrize("name", ["open", "fills"])
@pytest.mark.parametrize(
    "second_page,reason",
    [
        (success({"list": [order_row()], "cursor": "same-cursor"}), "repeated"),
        (success({"list": [order_row()], "cursor": 42}), "invalid"),
        (success({"list": None, "cursor": ""}), "incomplete"),
        ({"code": "45001", "msg": "synthetic page failure", "data": None}, "synthetic page failure"),
    ],
)
async def test_uta_pagination_failure_never_returns_partial_results(
    httpx_mock: HTTPXMock, name: str, second_page: dict, reason: str
) -> None:
    httpx_mock.add_response(
        json=success({"list": [order_row() if name == "open" else fill_row()], "cursor": "same-cursor"})
    )
    httpx_mock.add_response(json=second_page)
    async with Bitget(account_type="uta", market_type="linear") as ex:
        with pytest.raises(ExchangeError, match=reason):
            if name == "open":
                await ex.fetch_open_orders(SYMBOL)
            else:
                await ex.fetch_my_trades(SYMBOL)
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.parametrize("limit", [0, -1])
async def test_uta_nonpositive_fill_limit_rejects_before_get(httpx_mock: HTTPXMock, limit: int) -> None:
    async with Bitget(account_type="uta") as ex:
        with pytest.raises(ValueError, match="limit must be positive"):
            await ex.fetch_my_trades("BTC/USDT", limit=limit)
    assert httpx_mock.get_requests() == []

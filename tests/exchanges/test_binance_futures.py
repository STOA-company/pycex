"""W6 USDT-M money contract: exact signed requests and uncertain responses."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import ExchangeError, InsufficientBalanceError, InvalidOrderError, NetworkError, NotSupportedError
from pycex.exchanges.binance import Binance
from tests.exchanges.test_binance import _assert_valid_signature

SYMBOL = "BTC/USDT:USDT"


async def test_reduce_only_signed_request_and_native_fill(httpx_mock: HTTPXMock) -> None:
    raw = {
        "orderId": 7,
        "clientOrderId": "w6-close",
        "status": "PARTIALLY_FILLED",
        "origQty": "0.02",
        "executedQty": "0.01",
        "side": "SELL",
        "type": "LIMIT",
        "price": "60000",
    }
    httpx_mock.add_response(json=raw)
    async with Binance(api_key="k", secret="s", market_type="linear", sandbox=True) as ex:
        order = await ex.create_order(
            SYMBOL, "sell", "limit", 0.02, 60000, reduce_only=True, client_order_id="w6-close"
        )
    req = httpx_mock.get_request()
    assert req is not None
    assert req.url.path == "/fapi/v1/order" and "testnet" in req.url.host
    assert dict(req.url.params)["reduceOnly"] == "true"
    assert req.url.params["newClientOrderId"] == "w6-close"
    assert req.url.params["quantity"] == "0.02" and req.url.params["timeInForce"] == "GTC"
    assert "positionSide" not in req.url.params
    _assert_valid_signature(req.url.query.decode())
    assert order.status == "PARTIALLY_FILLED" and order.filled == 0.01 and order.raw == raw


@pytest.mark.parametrize("market_type", ["spot", "linear"])
async def test_default_order_omits_reduce_only(httpx_mock: HTTPXMock, market_type: str) -> None:
    httpx_mock.add_response(json={"orderId": 7})
    async with Binance(api_key="k", secret="s", market_type=market_type) as ex:
        order = await ex.create_order(SYMBOL if market_type == "linear" else "BTC/USDT", "buy", "market", 0.01)
    req = httpx_mock.get_request()
    assert req is not None and "reduceOnly" not in req.url.params
    assert order.status == "" and order.filled == 0


async def test_spot_reduce_only_and_non_boolean_rejected_before_http(httpx_mock: HTTPXMock) -> None:
    async with Binance() as ex:
        with pytest.raises(NotSupportedError):
            await ex.create_order("BTC/USDT", "sell", "market", 1, reduce_only=True)
        with pytest.raises(InvalidOrderError):
            await ex.create_order("BTC/USDT", "sell", "market", 1, reduce_only="false")
    assert httpx_mock.get_requests() == []


async def test_set_leverage_only_and_exact_ack(httpx_mock: HTTPXMock) -> None:
    raw = {"symbol": "BTCUSDT", "leverage": 200, "maxNotionalValue": "10000"}
    httpx_mock.add_response(json=raw)
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        assert await ex.set_leverage(SYMBOL, 200.0) == raw
    req = httpx_mock.get_request()
    assert req is not None and req.url.path == "/fapi/v1/leverage"
    assert req.url.params["symbol"] == "BTCUSDT" and req.url.params["leverage"] == "200"
    assert req.headers["X-MBX-APIKEY"] == "k"
    _assert_valid_signature(req.url.query.decode())


@pytest.mark.parametrize("status", [200, 400])
async def test_explicit_margin_mode_unchanged_then_leverage(httpx_mock: HTTPXMock, status: int) -> None:
    httpx_mock.add_response(status_code=status, json={"code": -4046, "msg": "No need to change margin type."})
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "leverage": 3})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        assert (await ex.set_leverage(SYMBOL, 3, "isolated"))["leverage"] == 3
    requests = httpx_mock.get_requests()
    assert [r.url.path for r in requests] == ["/fapi/v1/marginType", "/fapi/v1/leverage"]
    assert requests[0].url.params["marginType"] == "ISOLATED"
    for req in requests:
        _assert_valid_signature(req.url.query.decode())


@pytest.mark.parametrize("mode,native", [("cross", "CROSSED"), ("isolated", "ISOLATED")])
async def test_margin_mode_wire_contract(httpx_mock: HTTPXMock, mode: str, native: str) -> None:
    httpx_mock.add_response(json={"code": 200, "msg": "success"})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        assert (await ex.set_margin_mode(SYMBOL, mode))["code"] == 200
    req = httpx_mock.get_request()
    assert req is not None and req.url.params["marginType"] == native
    _assert_valid_signature(req.url.query.decode())


@pytest.mark.parametrize("status", [200, 400])
async def test_margin_failure_stops_before_leverage(httpx_mock: HTTPXMock, status: int) -> None:
    httpx_mock.add_response(status_code=status, json={"code": -4048, "msg": "Margin type cannot be changed"})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError) as err:
            await ex.set_leverage(SYMBOL, 3, "isolated")
        assert str(err.value.code) == "-4048"
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("lever", [0, -1, 1.5, float("nan"), float("inf"), True])
async def test_invalid_leverage_has_no_side_effects(httpx_mock: HTTPXMock, lever: float) -> None:
    async with Binance(market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.set_leverage(SYMBOL, lever, "isolated")
    assert httpx_mock.get_requests() == []


async def test_spot_and_bad_mode_setting_rejected_before_http(httpx_mock: HTTPXMock) -> None:
    async with Binance() as ex:
        with pytest.raises(NotSupportedError):
            await ex.set_leverage("BTC/USDT", 3)
        with pytest.raises(NotSupportedError):
            await ex.set_margin_mode("BTC/USDT", "isolated")
    async with Binance(market_type="linear") as ex:
        with pytest.raises(InvalidOrderError):
            await ex.set_leverage(SYMBOL, 3, "invented")
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    "raw", [{}, {"leverage": 3}, {"leverage": 2, "symbol": "BTCUSDT"}, {"leverage": 3, "symbol": "ETHUSDT"}]
)
async def test_leverage_unknown_or_mismatched_ack_rejected(httpx_mock: HTTPXMock, raw: dict) -> None:
    httpx_mock.add_response(json=raw)
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError, match="acknowledgement"):
            await ex.set_leverage(SYMBOL, 3)


@pytest.mark.parametrize("action", ["create", "fetch", "cancel"])
async def test_empty_order_ack_is_unknown(httpx_mock: HTTPXMock, action: str) -> None:
    httpx_mock.add_response(json={"orderId": None, "status": None})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        if action == "create":
            order = await ex.create_order(SYMBOL, "sell", "market", 0.01, reduce_only=True)
        elif action == "fetch":
            order = await ex.fetch_order(None, SYMBOL, client_order_id="w6-close")
        else:
            order = await ex.cancel_order("7", SYMBOL)
    assert order.id == "" and order.status == "" and order.filled == 0


async def test_http_200_rejection_not_an_order(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": -2022, "msg": "ReduceOnly Order is rejected."})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError) as err:
            await ex.create_order(SYMBOL, "sell", "market", 0.01, reduce_only=True)
        assert str(err.value.code) == "-2022"
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("code", [-2018, -2019])
async def test_futures_insufficient_margin_is_typed(httpx_mock: HTTPXMock, status: int, code: int) -> None:
    httpx_mock.add_response(status_code=status, json={"code": code, "msg": "Margin is insufficient."})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(InsufficientBalanceError) as err:
            await ex.create_order(SYMBOL, "buy", "market", 0.01)
    assert str(err.value.code) == str(code)


async def test_leverage_failure_after_mode_success_is_visible(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": 200, "msg": "success"})
    httpx_mock.add_response(status_code=400, json={"code": -4028, "msg": "Leverage is not valid"})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(ExchangeError) as err:
            await ex.set_leverage(SYMBOL, 1000, "isolated")
    assert str(err.value.code) == "-4028"
    assert len(httpx_mock.get_requests()) == 2


async def test_lost_response_reconcile_same_client_id_without_recreate(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("synthetic lost response"))
    httpx_mock.add_response(json={"orderId": 7, "clientOrderId": "w6-close", "status": "NEW", "origQty": "0.01"})
    async with Binance(api_key="k", secret="s", market_type="linear") as ex:
        with pytest.raises(NetworkError):
            await ex.create_order(SYMBOL, "sell", "market", 0.01, reduce_only=True, client_order_id="w6-close")
        order = await ex.fetch_order(None, SYMBOL, client_order_id="w6-close")
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["POST", "GET"]
    assert requests[1].url.params["origClientOrderId"] == "w6-close"
    assert order.id == "7" and order.status == "NEW" and order.filled == 0


def test_sync_futures_order_and_leverage(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "leverage": 3})
    httpx_mock.add_response(json={"orderId": 7})
    ex = Binance(api_key="k", secret="s", market_type="linear")
    assert ex.set_leverage_sync(SYMBOL, 3)["leverage"] == 3
    assert ex.create_order_sync(SYMBOL, "sell", "market", 0.01, reduce_only=True).status == ""
    assert len(httpx_mock.get_requests()) == 2

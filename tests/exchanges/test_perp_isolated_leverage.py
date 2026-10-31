"""Binance fapi + Bitget mix: isolated margin, leverage set/read and reduce-only (contract mock).

Request shapes follow the official docs (no network):
- Binance USDT-M: POST /fapi/v1/marginType (ISOLATED/CROSSED; -4046 "No need to change margin type"),
  POST /fapi/v1/leverage (integer), GET /fapi/v2/positionRisk, POST /fapi/v1/order reduceOnly=true;
  -4161 "Leverage reduction is not supported in Isolated Margin Mode with open positions".
  https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api
- Bitget mix v2: POST /api/v2/mix/account/set-margin-mode, /set-leverage, GET /account/account,
  POST /order/place-order marginMode + reduceOnly=YES (one-way mode).
  https://www.bitget.com/api-doc/contract/account/Change-Margin-Mode
"""

from __future__ import annotations

import json as json_lib
from urllib.parse import parse_qs

import pytest
from pytest_httpx import HTTPXMock

import pycex.exchanges.bitget as bg
from pycex.exceptions import ExchangeError, InvalidOrderError, NotSupportedError, RateLimitError
from pycex.exchanges.binance import Binance
from pycex.exchanges.bitget import Bitget

SYM = "BTC/USDT:USDT"


def _bn() -> Binance:
    return Binance(api_key="k", secret="s", market_type="linear")


def _bg() -> Bitget:
    return Bitget(api_key="k", secret="s", passphrase="p", market_type="linear")


def _q(req) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}


@pytest.fixture(autouse=True)
def _clear_uta():
    bg._UTA_KEYS.clear()
    yield
    bg._UTA_KEYS.clear()


# ── Binance ──


async def test_binance_set_leverage_sets_isolated_then_leverage(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": 200, "msg": "success"})
    httpx_mock.add_response(json={"leverage": 3, "maxNotionalValue": "1000000", "symbol": "BTCUSDT"})
    ex = _bn()
    row = await ex.set_leverage(SYM, 3, "isolated")
    r1, r2 = httpx_mock.get_requests()
    assert r1.url.path == "/fapi/v1/marginType"
    assert _q(r1)["marginType"] == "ISOLATED" and _q(r1)["symbol"] == "BTCUSDT" and "signature" in _q(r1)
    assert r2.url.path == "/fapi/v1/leverage"
    assert _q(r2)["leverage"] == "3" and _q(r2)["symbol"] == "BTCUSDT"
    assert row["leverage"] == 3
    await ex.close()


async def test_binance_already_isolated_4046_is_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"code": -4046, "msg": "No need to change margin type."})
    httpx_mock.add_response(json={"leverage": 5, "symbol": "BTCUSDT"})
    ex = _bn()
    assert (await ex.set_leverage(SYM, 5, "isolated"))["leverage"] == 5
    await ex.close()


async def test_binance_margin_type_other_error_raises_and_no_leverage_call(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        status_code=400, json={"code": -4047, "msg": "Margin type cannot be changed if there exists open orders."}
    )
    ex = _bn()
    with pytest.raises(ExchangeError) as ei:
        await ex.set_leverage(SYM, 3, "isolated")
    assert ei.value.code == -4047
    assert len(httpx_mock.get_requests()) == 1
    await ex.close()


async def test_binance_leverage_reduction_with_position_4161_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"code": -4046, "msg": "No need to change margin type."})
    httpx_mock.add_response(
        status_code=400,
        json={"code": -4161, "msg": "Leverage reduction is not supported in Isolated Margin Mode with open positions."},
    )
    ex = _bn()
    with pytest.raises(ExchangeError) as ei:
        await ex.set_leverage(SYM, 2, "isolated")
    assert ei.value.code == -4161
    await ex.close()


@pytest.mark.parametrize("bad", [0, 0.5, 2.5, float("nan"), float("inf"), -3])
async def test_binance_non_integer_leverage_rejected_before_any_request(bad: float) -> None:
    ex = _bn()
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage(SYM, bad, "isolated")
    await ex.close()


async def test_binance_spot_leverage_not_supported() -> None:
    ex = Binance(api_key="k", secret="s")
    with pytest.raises(NotSupportedError):
        await ex.set_leverage(SYM, 3, "isolated")
    with pytest.raises(NotSupportedError):
        await ex.fetch_leverage(SYM)
    await ex.close()


async def test_binance_fetch_leverage_reads_position_risk(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[{"symbol": "BTCUSDT", "positionAmt": "0.000", "leverage": "3", "marginType": "isolated"}]
    )
    ex = _bn()
    rows = await ex.fetch_leverage(SYM, "isolated")
    req = httpx_mock.get_requests()[-1]
    assert req.url.path == "/fapi/v2/positionRisk" and _q(req)["symbol"] == "BTCUSDT"
    assert rows[0]["mgnMode"] == "isolated" and rows[0]["lever"] == "3"
    await ex.close()


async def test_binance_fetch_leverage_empty_is_error_not_default(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[])
    ex = _bn()
    with pytest.raises(ExchangeError):
        await ex.fetch_leverage(SYM)
    await ex.close()


async def test_binance_reduce_only_order_and_client_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "orderId": 1,
            "symbol": "BTCUSDT",
            "status": "NEW",
            "side": "SELL",
            "type": "LIMIT",
            "origQty": "0.01",
            "price": "60000",
            "executedQty": "0",
            "clientOrderId": "cid1",
        }
    )
    ex = _bn()
    await ex.create_order(SYM, "sell", "limit", 0.01, 60000, client_order_id="cid1", reduce_only=True)
    q = _q(httpx_mock.get_requests()[-1])
    assert q["reduceOnly"] == "true" and q["newClientOrderId"] == "cid1" and q["side"] == "SELL"
    await ex.close()


async def test_binance_order_without_reduce_only_has_no_flag(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "orderId": 1,
            "symbol": "BTCUSDT",
            "status": "NEW",
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "0.01",
            "price": "1",
            "executedQty": "0",
        }
    )
    ex = _bn()
    await ex.create_order(SYM, "buy", "limit", 0.01, 1)
    assert "reduceOnly" not in _q(httpx_mock.get_requests()[-1])
    await ex.close()


async def test_binance_spot_reduce_only_refused() -> None:
    ex = Binance(api_key="k", secret="s")
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT", "sell", "limit", 0.01, 1, reduce_only=True)
    await ex.close()


async def test_binance_rate_limit_and_auth_errors_surface(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=429, json={"code": -1003, "msg": "Too many requests"})
    ex = _bn()
    with pytest.raises(RateLimitError):
        await ex.set_leverage(SYM, 3, "isolated")
    await ex.close()


# ── Bitget ──


async def test_bitget_set_margin_mode_and_leverage_bodies(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": {"marginMode": "isolated"}})
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": {"leverage": "3"}})
    ex = _bg()
    await ex.set_margin_mode(SYM, "isolated")
    row = await ex.set_leverage(SYM, 3, "isolated")
    r1, r2 = httpx_mock.get_requests()
    assert r1.url.path == "/api/v2/mix/account/set-margin-mode"
    assert json_lib.loads(r1.content) == {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "isolated",
    }
    assert r2.url.path == "/api/v2/mix/account/set-leverage"
    assert json_lib.loads(r2.content) == {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "leverage": "3",
    }
    assert row["leverage"] == "3"
    assert r1.headers["ACCESS-SIGN"] and r2.headers["ACCESS-SIGN"]
    await ex.close()


async def test_bitget_set_leverage_hold_side_and_fraction(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": {}}, is_reusable=True)
    ex = _bg()
    await ex.set_leverage(SYM, 2.5, "isolated", hold_side="long")
    body = json_lib.loads(httpx_mock.get_requests()[-1].content)
    assert body["leverage"] == "2.5" and body["holdSide"] == "long"
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage(SYM, 3, "isolated", hold_side="both")
    await ex.close()


async def test_bitget_margin_mode_change_refused_is_raised(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "40872", "msg": "Cannot change margin mode with open position", "data": None})
    ex = _bg()
    with pytest.raises(ExchangeError) as ei:
        await ex.set_margin_mode(SYM, "isolated")
    assert ei.value.code == "40872"
    await ex.close()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
async def test_bitget_bad_leverage_rejected_before_request(bad: float) -> None:
    ex = _bg()
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage(SYM, bad)
    with pytest.raises(InvalidOrderError):
        await ex.set_margin_mode(SYM, "weird")
    await ex.close()


async def test_bitget_fetch_leverage_isolated_and_cross(httpx_mock: HTTPXMock) -> None:
    acct = {
        "symbol": "BTCUSDT",
        "marginMode": "isolated",
        "crossedMarginLeverage": 20,
        "isolatedLongLever": 3,
        "isolatedShortLever": 3,
    }
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": acct}, is_reusable=True)
    ex = _bg()
    rows = await ex.fetch_leverage(SYM, "isolated")
    req = httpx_mock.get_requests()[-1]
    assert req.url.path == "/api/v2/mix/account/account" and _q(req)["symbol"] == "BTCUSDT"
    assert rows[0]["mgnMode"] == "isolated" and rows[0]["lever"] == "3"
    assert (await ex.fetch_leverage(SYM, "cross"))[0]["lever"] == "20"
    await ex.close()


async def test_bitget_fetch_leverage_split_long_short_has_no_single_lever(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"code": "00000", "data": {"marginMode": "isolated", "isolatedLongLever": 3, "isolatedShortLever": 5}}
    )
    ex = _bg()
    assert (await ex.fetch_leverage(SYM, "isolated"))[0]["lever"] is None
    await ex.close()


async def test_bitget_order_isolated_reduce_only_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": {"orderId": "9", "clientOid": "c1"}})
    ex = _bg()
    await ex.create_order(
        SYM, "sell", "limit", 0.01, 60000, client_order_id="c1", reduce_only=True, margin_mode="isolated"
    )
    body = json_lib.loads(httpx_mock.get_requests()[-1].content)
    assert body["marginMode"] == "isolated" and body["reduceOnly"] == "YES" and body["clientOid"] == "c1"
    await ex.close()


async def test_bitget_order_default_unchanged_crossed_no_reduce(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": {"orderId": "9"}})
    ex = _bg()
    await ex.create_order(SYM, "buy", "limit", 0.01, 1)
    body = json_lib.loads(httpx_mock.get_requests()[-1].content)
    assert body["marginMode"] == "crossed" and "reduceOnly" not in body
    await ex.close()


async def test_bitget_spot_rejects_perp_only_args_and_leverage() -> None:
    ex = Bitget(api_key="k", secret="s", passphrase="p")
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT", "sell", "limit", 0.01, 1, reduce_only=True)
    with pytest.raises(NotSupportedError):
        await ex.set_leverage("BTC/USDT", 3)
    with pytest.raises(NotSupportedError):
        await ex.set_margin_mode("BTC/USDT")
    await ex.close()


async def test_bitget_sandbox_uses_s_prefixed_symbols(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "data": {}})
    ex = Bitget(api_key="k", secret="s", passphrase="p", market_type="linear", sandbox=True)
    await ex.set_leverage(SYM, 3, "isolated")
    body = json_lib.loads(httpx_mock.get_requests()[-1].content)
    assert body["symbol"] == "SBTCSUSDT" and body["productType"] == "SUSDT-FUTURES"
    await ex.close()


async def test_bitget_uta_key_isolated_refused_and_reduce_only_yes(httpx_mock: HTTPXMock) -> None:
    bg._UTA_KEYS.add("k")
    ex = _bg()
    with pytest.raises(NotSupportedError):
        await ex.create_order(SYM, "buy", "limit", 0.01, 1, margin_mode="isolated")
    httpx_mock.add_response(json={"code": "00000", "data": {"orderId": "1", "clientOid": "c"}})
    await ex.create_order(SYM, "sell", "limit", 0.01, 1, client_order_id="c", reduce_only=True)
    body = json_lib.loads(httpx_mock.get_requests()[-1].content)
    assert body["reduceOnly"] == "yes" and body["marginMode"] == "crossed"
    await ex.close()


async def test_bitget_ensure_margin_mode_switches_only_when_different(httpx_mock: HTTPXMock) -> None:
    acct = {"marginMode": "crossed", "crossedMarginLeverage": 20, "isolatedLongLever": 3, "isolatedShortLever": 3}
    httpx_mock.add_response(json={"code": "00000", "data": acct})
    httpx_mock.add_response(json={"code": "00000", "data": {"marginMode": "isolated"}})
    httpx_mock.add_response(json={"code": "00000", "data": {"leverage": "3"}})
    ex = _bg()
    await ex.set_leverage(SYM, 3, "isolated", ensure_margin_mode=True)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths == [
        "/api/v2/mix/account/account",
        "/api/v2/mix/account/set-margin-mode",
        "/api/v2/mix/account/set-leverage",
    ]
    await ex.close()


async def test_bitget_ensure_margin_mode_skips_when_already_isolated(httpx_mock: HTTPXMock) -> None:
    acct = {"marginMode": "isolated", "isolatedLongLever": 3, "isolatedShortLever": 3}
    httpx_mock.add_response(json={"code": "00000", "data": acct})
    httpx_mock.add_response(json={"code": "00000", "data": {"leverage": "3"}})
    ex = _bg()
    await ex.set_leverage(SYM, 3, "isolated", ensure_margin_mode=True)
    assert [r.url.path for r in httpx_mock.get_requests()][-1] == "/api/v2/mix/account/set-leverage"
    assert len(httpx_mock.get_requests()) == 2
    await ex.close()

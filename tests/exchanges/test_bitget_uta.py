"""Bitget Unified Trading Account (UTA, v3) contract-mock tests.

The fake server below answers with the response shapes printed in Bitget's official UTA docs
(https://www.bitget.com/docs/uta/catalog ; place-order, cancel-order, order-info, unfilled-orders, fills,
account/assets, position/current-position). Classic keys must keep the exact v2 request sequence
(golden file recorded from the base commit).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    AuthenticationError,
    DuplicateOrderError,
    ExchangeError,
    InsufficientBalanceError,
    NetworkError,
)
from pycex.exchanges import bitget as bg
from pycex.exchanges.bitget import Bitget

GOLDEN = Path(__file__).parent.parent / "fixtures" / "bitget" / "classic_calls_golden.json"


@pytest.fixture(autouse=True)
def _frozen(monkeypatch: pytest.MonkeyPatch) -> Any:
    import pycex.auth as auth

    monkeypatch.setattr(auth, "timestamp_ms", lambda: 1760000000000)
    getattr(bg, "_UTA_KEYS", set()).clear()
    yield
    getattr(bg, "_UTA_KEYS", set()).clear()


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "00000", "msg": "success", "requestTime": 1, "data": data})


def _err(code: str, status: int = 400, msg: str = "err") -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg, "requestTime": 1, "data": None})


class Fake:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.accepted: dict[str, str] = {}
        self.place_script: list[Any] = []
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        q = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        if path.startswith("/api/v2/"):
            if self.mode == "uta":
                return _err("40085", msg="Unified Account mode, Classic Account API not supported")
            return self._v2(path, q, body)
        return self._v3(path, q, body)

    def _v2(self, path: str, q: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        if path.endswith("/account/assets"):
            return _ok([{"coin": "USDT", "available": "100", "frozen": "1", "locked": "0"}])
        if path.endswith("/mix/account/accounts"):
            return _ok([{"marginCoin": "USDT", "available": "100", "locked": "1"}])
        if path.endswith("/mix/position/all-position"):
            return _ok(
                [
                    {
                        "symbol": "BTCUSDT",
                        "holdSide": "long",
                        "total": "0.01",
                        "openPriceAvg": "60000",
                        "unrealizedPL": "5",
                        "leverage": "3",
                        "liquidationPrice": "40000",
                        "cTime": "1",
                    }
                ]
            )
        if path.endswith("/place-order"):
            return _ok({"orderId": "100", "clientOid": body.get("clientOid", "")})
        if path.endswith("/cancel-order"):
            return _ok({"orderId": body.get("orderId", ""), "clientOid": ""})
        if path.endswith("/orders-pending"):
            return _ok(
                {
                    "entrustedList": [
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "100",
                            "side": "buy",
                            "orderType": "limit",
                            "size": "0.01",
                            "price": "50000",
                            "baseVolume": "0",
                            "status": "live",
                            "cTime": "1",
                        }
                    ]
                }
            )
        if path.endswith("/unfilled-orders"):
            return _ok(
                [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": "100",
                        "side": "buy",
                        "orderType": "limit",
                        "size": "0.01",
                        "price": "50000",
                        "baseVolume": "0",
                        "status": "live",
                        "cTime": "1",
                    }
                ]
            )
        if path.endswith("/order/detail") or path.endswith("/orderInfo"):
            return _ok(
                [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": "100",
                        "side": "buy",
                        "orderType": "limit",
                        "size": "0.01",
                        "price": "50000",
                        "baseVolume": "0",
                        "state": "live",
                        "status": "live",
                        "cTime": "1",
                    }
                ]
            )
        if path.endswith("/order/fills"):
            return _ok(
                {
                    "fillList": [
                        {
                            "tradeId": "t1",
                            "orderId": "100",
                            "side": "buy",
                            "price": "60000",
                            "baseVolume": "0.01",
                            "feeDetail": [{"feeCoin": "USDT", "totalFee": "0.1"}],
                            "cTime": "1",
                        }
                    ]
                }
            )
        if path.endswith("/trade/fills"):
            return _ok(
                [
                    {
                        "tradeId": "t1",
                        "orderId": "100",
                        "side": "buy",
                        "priceAvg": "60000",
                        "size": "0.01",
                        "feeDetail": {"feeCoin": "USDT", "totalFee": "0.1"},
                        "cTime": "1",
                    }
                ]
            )
        return _err("40404", 404)

    def _v3(self, path: str, q: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        if path == "/api/v3/account/assets":
            return _ok(
                {
                    "accountEquity": "11",
                    "usdtEquity": "11",
                    "assets": [
                        {
                            "coin": "USDT",
                            "equity": "100",
                            "balance": "100",
                            "available": "99",
                            "locked": "1",
                            "debt": "0",
                        },
                        {"coin": "BGB", "equity": "0", "balance": "0", "available": "0", "locked": "0"},
                    ],
                }
            )
        if path == "/api/v3/position/current-position":
            assert q["category"] == "USDT-FUTURES"
            return _ok(
                {
                    "list": [
                        {
                            "category": "USDT-FUTURES",
                            "symbol": "BTCUSDT",
                            "posSide": "short",
                            "total": "0.02",
                            "avgPrice": "60000",
                            "unrealisedPnl": "-3",
                            "leverage": "3",
                            "liquidationPrice": "80000",
                            "createdTime": "1736378720620",
                        }
                    ]
                }
            )
        if path == "/api/v3/trade/place-order":
            if self.place_script:
                step = self.place_script.pop(0)
                if step == "net":
                    raise httpx.ReadTimeout("t")
                if step[0] == "accepted":
                    self.accepted[body["clientOid"]] = "9001"
                    return _err(step[1], step[2])
                return _err(step[0], step[1])
            if body["clientOid"] in self.accepted:
                return _err("25212", msg="Duplicate clientOid")
            self.accepted[body["clientOid"]] = "9001"
            return _ok({"orderId": "9001", "clientOid": body["clientOid"]})
        if path == "/api/v3/trade/order-info":
            oid = q.get("clientOid") or ""
            if q.get("orderId") or oid in self.accepted:
                return _ok(
                    {
                        "orderId": q.get("orderId") or self.accepted[oid],
                        "clientOid": oid,
                        "category": "USDT-FUTURES",
                        "symbol": "BTCUSDT",
                        "side": "buy",
                        "orderType": "limit",
                        "price": "50000",
                        "qty": "0.01",
                        "cumExecQty": "0.004",
                        "cumExecValue": "200",
                        "orderStatus": "partially_filled",
                        "createdTime": "1",
                    }
                )
            return _err("25204", msg="Order does not exist")
        if path == "/api/v3/trade/cancel-order":
            return _ok({"orderId": body.get("orderId", ""), "clientOid": ""})
        if path == "/api/v3/trade/unfilled-orders":
            return _ok(
                {
                    "list": [
                        {
                            "orderId": "9001",
                            "clientOid": "c",
                            "symbol": "BTCUSDT",
                            "side": "sell",
                            "orderType": "limit",
                            "price": "70000",
                            "qty": "0.01",
                            "cumExecQty": "0",
                            "orderStatus": "new",
                            "createdTime": "1",
                        }
                    ],
                    "cursor": "9001",
                }
            )
        if path == "/api/v3/trade/fills":
            return _ok(
                {
                    "list": [
                        {
                            "execId": "e1",
                            "orderId": "9001",
                            "symbol": "BTCUSDT",
                            "side": "buy",
                            "execPrice": "60000",
                            "execQty": "0.01",
                            "feeDetail": [{"feeCoin": "USDT", "fee": "0.2"}],
                            "createdTime": "5",
                        },
                        {
                            "execId": "e2",
                            "orderId": "9002",
                            "symbol": "ETHUSDT",
                            "side": "buy",
                            "execPrice": "3000",
                            "execQty": "1",
                            "feeDetail": [{"feeCoin": "USDT", "fee": "0.1"}],
                            "createdTime": "6",
                        },
                    ],
                    "cursor": "e2",
                }
            )
        return _err("40404", 404)


def _sig_ok(r: httpx.Request, secret: str = "sec") -> None:
    msg = r.headers["ACCESS-TIMESTAMP"] + r.method + r.url.raw_path.decode() + (r.content.decode() if r.content else "")
    assert (
        r.headers["ACCESS-SIGN"]
        == base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    )


async def _classic_script(mt: str) -> list[Any]:
    ex = Bitget("key", "sec", "pp", market_type=mt)  # type: ignore[arg-type]
    sym = "BTC/USDT:USDT" if mt == "linear" else "BTC/USDT"
    out: list[Any] = []
    out.append(await ex.fetch_balance())
    if mt == "linear":
        out.append(await ex.fetch_positions())
    out.append(await ex.create_order(sym, "buy", "market", 0.01, client_order_id="cid1"))
    out.append(await ex.create_order(sym, "sell", "limit", 0.01, 61000.0, client_order_id="cid2"))
    out.append(await ex.cancel_order("100", sym))
    out.append(await ex.fetch_order("100", sym))
    out.append(await ex.fetch_open_orders(sym))
    out.append(await ex.fetch_my_trades(sym, limit=5))
    return out


def _calls(fake: Fake) -> list[list[str]]:
    return [
        [
            r.method,
            r.url.raw_path.decode(),
            r.content.decode(),
            r.headers["ACCESS-SIGN"][:16],
            r.headers["ACCESS-TIMESTAMP"],
        ]
        for r in fake.seen
    ]


@pytest.mark.parametrize("mt", ["spot", "linear"])
async def test_classic_request_sequence_identical_to_base(httpx_mock: HTTPXMock, mt: str) -> None:
    fake = Fake("classic")
    httpx_mock.add_callback(fake, is_reusable=True)
    res = await _classic_script(mt)
    calls = _calls(fake)
    if os.environ.get("WRITE_BITGET_GOLDEN"):
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[mt] = {
            "calls": calls,
            "results": json.loads(
                json.dumps([getattr(r, "model_dump", lambda: r)() for r in res], default=str, sort_keys=True)
            ),
        }
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True))
        return
    golden = json.loads(GOLDEN.read_text())[mt]
    assert calls == golden["calls"]
    got = json.loads(json.dumps([getattr(r, "model_dump", lambda: r)() for r in res], default=str, sort_keys=True))
    assert got == golden["results"]
    assert not bg._UTA_KEYS


async def test_40085_switches_key_to_v3_and_caches(httpx_mock: HTTPXMock) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="linear")
    bal = await ex.fetch_balance()
    assert [r.url.path for r in fake.seen] == ["/api/v2/mix/account/accounts", "/api/v3/account/assets"]
    assert [(a.asset, a.free, a.locked) for a in bal.assets] == [("USDT", 99.0, 1.0)]
    n = len(fake.seen)
    pos = await Bitget("key", "sec", "pp", market_type="linear").fetch_positions()
    assert [r.url.path for r in fake.seen[n:]] == ["/api/v3/position/current-position"]
    assert (pos[0].symbol, pos[0].side, pos[0].amount, pos[0].entry_price, pos[0].unrealized_pnl) == (
        "BTC/USDT:USDT",
        "short",
        0.02,
        60000.0,
        -3.0,
    )
    for r in fake.seen:
        _sig_ok(r)


async def test_uta_order_roundtrip_linear(httpx_mock: HTTPXMock) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="linear")
    o = await ex.create_order("BTC/USDT:USDT", "buy", "limit", 0.01, 50000.0, client_order_id="my-1")
    assert o.id == "9001" and o.client_order_id == "my-1"
    place = [r for r in fake.seen if r.url.path == "/api/v3/trade/place-order"][0]
    assert json.loads(place.content) == {
        "category": "USDT-FUTURES",
        "symbol": "BTCUSDT",
        "qty": "0.01",
        "side": "buy",
        "orderType": "limit",
        "clientOid": "my-1",
        "marginMode": "crossed",
        "timeInForce": "gtc",
        "price": "50000.0",
    }
    _sig_ok(place)
    f = await ex.fetch_order(None, "BTC/USDT:USDT", client_order_id="my-1")
    assert (f.status, f.filled, f.amount) == ("partially_filled", 0.004, 0.01)
    oo = await ex.fetch_open_orders("BTC/USDT:USDT")
    assert (oo[0].id, oo[0].status, oo[0].side) == ("9001", "live", "sell")
    c = await ex.cancel_order("9001", "BTC/USDT:USDT")
    assert c.id == "9001"
    assert json.loads([r for r in fake.seen if r.url.path.endswith("cancel-order")][-1].content) == {
        "category": "USDT-FUTURES",
        "orderId": "9001",
    }
    mt = await ex.fetch_my_trades("BTC/USDT:USDT")
    assert [(t.id, t.price, t.amount, t.fee) for t in mt] == [("e1", 60000.0, 0.01, 0.2)]  # ETH fill filtered out


async def test_uta_spot_market_buy_uses_quote_qty(httpx_mock: HTTPXMock) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="spot")
    o = await ex.create_order("BTC/USDT", "buy", "market", 25.0)
    assert o.amount_quote is not None and o.client_order_id and o.client_order_id.startswith("q")
    body = json.loads([r for r in fake.seen if r.url.path == "/api/v3/trade/place-order"][0].content)
    assert body["category"] == "SPOT" and body["qty"] == "25.0" and "marginMode" not in body


async def test_same_client_id_resend_is_duplicate_error(httpx_mock: HTTPXMock) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="linear")
    await ex.create_order("BTC/USDT:USDT", "buy", "market", 0.01, client_order_id="same")
    with pytest.raises(DuplicateOrderError):
        await ex.create_order("BTC/USDT:USDT", "buy", "market", 0.01, client_order_id="same")
    assert len(fake.accepted) == 1


@pytest.mark.parametrize(
    "script", [["net"], [("45001", 500)], [("40725", 500)], [("40010", 400)], [("accepted", "45001", 500)]]
)
async def test_unknown_outcome_never_resends(httpx_mock: HTTPXMock, script: list[Any]) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="linear")
    await ex.fetch_balance()
    fake.place_script = list(script)
    n_before = len([r for r in fake.seen if r.url.path.endswith("place-order")])
    if script[0][0] == "accepted":
        o = await ex.create_order("BTC/USDT:USDT", "buy", "market", 0.01, client_order_id="u1")
        assert o.id == "9001"  # recovered through clientOid lookup
    else:
        with pytest.raises((ExchangeError, NetworkError, AuthenticationError)):
            await ex.create_order("BTC/USDT:USDT", "buy", "market", 0.01, client_order_id="u1")
    assert len([r for r in fake.seen if r.url.path.endswith("place-order")]) - n_before == 1


@pytest.mark.parametrize("code", ["25202", "25203"])
async def test_insufficient_balance_code_not_resent(httpx_mock: HTTPXMock, code: str) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="linear")
    await ex.fetch_balance()
    fake.place_script = [(code, 400)]
    with pytest.raises(InsufficientBalanceError) as ei:
        await ex.create_order("BTC/USDT:USDT", "buy", "market", 1.0)
    assert ei.value.code == code  # D6477: pycex 는 줄여 다시 없음 — 분류까지만, 줄여 다시는 트레이더가 한다
    assert len([r for r in fake.seen if r.url.path.endswith("place-order")]) == 1


async def test_sandbox_never_auto_switches(httpx_mock: HTTPXMock) -> None:
    fake = Fake("uta")
    httpx_mock.add_callback(fake, is_reusable=True)
    ex = Bitget("key", "sec", "pp", market_type="spot", sandbox=True)
    with pytest.raises(ExchangeError):
        await ex.fetch_balance()
    assert not bg._UTA_KEYS and all(r.url.path.startswith("/api/v2/") for r in fake.seen)

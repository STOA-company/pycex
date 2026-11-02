"""Offline HTTP contract tests, using official endpoint request/response shapes.

No exchange or demo network is reachable: every adapter receives MockTransport.
Docs: Binance core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade,
Bitget classic-contract-plan/classic-contract-plan, Bybit v5/order/create-order.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pycex.exceptions import ExchangeError, InvalidOrderError
from pycex.exchanges._attached import protection_client_order_id
from pycex.exchanges.binance import Binance
from pycex.exchanges.bitget import Bitget
from pycex.exchanges.bybit import Bybit
from pycex.models.market import Market

SYMBOL = "BTC/USDT:USDT"


def mock_exchange(cls: Any, handler: Any, **kwargs: Any) -> Any:
    ex = cls(api_key="offline-key", secret="offline-secret", market_type="linear", **kwargs)
    ex._markets["BTCUSDT"] = Market(
        symbol=SYMBOL,
        native="BTCUSDT",
        base="BTC",
        quote="USDT",
        market_type="linear",
        amount_step=0.001,
        price_tick=0.1,
    )
    ex._http.set_transport_factory(lambda: httpx.MockTransport(handler))
    return ex


def success(cls: Any, payload: Any) -> dict[str, Any] | list[Any]:
    if cls is Binance:
        return payload
    if cls is Bitget:
        return {"code": "00000", "data": payload}
    return {"retCode": 0, "result": payload}


@pytest.mark.parametrize("cls", [Binance, Bitget, Bybit])
@pytest.mark.parametrize("kwargs", [{"reduce_only": True}, {"market_type": "spot"}])
async def test_invalid_attachment_is_refused_before_entry(cls: Any, kwargs: Any) -> None:
    calls = []
    ex = cls(api_key="offline-key", secret="s", market_type=kwargs.get("market_type", "linear"))
    ex._http.set_transport_factory(lambda: httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(InvalidOrderError):
        await ex.create_order(
            SYMBOL if kwargs.get("market_type") != "spot" else "BTC/USDT",
            "buy",
            "market",
            1,
            reduce_only=kwargs.get("reduce_only", False),
            sl_px=97,
        )
    assert not calls
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget, Bybit])
@pytest.mark.parametrize("trigger", [0, -1, float("nan"), float("inf")])
async def test_invalid_trigger_never_places_entry(cls: Any, trigger: float) -> None:
    calls = []
    ex = mock_exchange(cls, lambda r: calls.append(r))
    with pytest.raises(InvalidOrderError):
        await ex.create_order(SYMBOL, "buy", "market", 1, sl_px=trigger)
    assert calls == []
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_entry_receipt_precedes_any_protection_write(cls: Any) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(
            200, json=success(cls, {"orderId": "parent", "clientOid": "intent1", "executedQty": "0.4", "origQty": "1"})
        )

    ex = mock_exchange(cls, handle)
    order = await ex.create_order(SYMBOL, "buy", "market", 1, client_order_id="intent1", tp_px=105, sl_px=97)
    assert order.id == "parent"
    assert order.raw["attached_exits"]["attached"] is False
    assert order.raw["attached_exits"]["status"] == "pending_fill"
    assert len(calls) == 1
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_fill_deltas_exact_and_duplicate_poll_sends_zero(cls: Any) -> None:
    bodies = []

    def handle(req: httpx.Request) -> httpx.Response:
        body = dict(req.url.params) if cls is Binance else json.loads(req.content)
        bodies.append(body)
        payload = {"algoId": str(len(bodies)), "algoStatus": "NEW"} if cls is Binance else {"orderId": str(len(bodies))}
        return httpx.Response(200, json=success(cls, payload))

    ex = mock_exchange(cls, handle)
    first = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", Decimal("0.4"), 105, 97)
    second = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", Decimal("0.7"), 105, 97, first)
    repeated = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", Decimal("0.7"), 105, 97, second)
    assert len(bodies) == 4
    assert [b.get("quantity", b.get("size")) for b in bodies] == ["0.4", "0.4", "0.3", "0.3"]
    assert len(repeated["legs"]) == 4
    assert repeated["status"] == "pending_confirmation"
    assert repeated["attached"] is False
    ids = [b.get("clientAlgoId", b.get("clientOid")) for b in bodies]
    assert len(set(ids)) == 4
    assert ids[0] == protection_client_order_id("intent1", "sl", "0.4")
    assert protection_client_order_id("intent1", "sl", "0.40") == ids[0]
    if cls is Binance:
        assert all(b["reduceOnly"] == "true" and b["workingType"] == "CONTRACT_PRICE" for b in bodies)
        assert bodies[0]["type"] == "STOP_MARKET"
        assert bodies[1]["type"] == "TAKE_PROFIT_MARKET"
        assert all(b["side"] == "SELL" and b["algoType"] == "CONDITIONAL" for b in bodies)
        assert all("closePosition" not in b for b in bodies)
    else:
        assert bodies[0]["planType"] == "loss_plan" and bodies[1]["planType"] == "profit_plan"
        assert all(
            b["executePrice"] == "0" and b["holdSide"] == "buy" and b["triggerType"] == "fill_price" for b in bodies
        )
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_unfilled_parent_has_no_child_write(cls: Any) -> None:
    calls = []
    ex = mock_exchange(cls, lambda r: calls.append(r))
    result = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", 0, 105, 97)
    assert result["status"] == "pending_fill" and not result["attached"]
    assert calls == []
    await ex.close()


@pytest.mark.parametrize("cls,codes", [(Binance, ["-2021", "-2022", "-1111"]), (Bitget, ["43034", "43023", "43028"])])
async def test_rejections_preserve_reason_and_do_not_retry(cls: Any, codes: list[str]) -> None:
    for code in codes:
        calls = []

        def handle(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            payload = (
                {"code": int(code), "msg": "invalid trigger/position/precision"}
                if cls is Binance
                else {"code": code, "msg": "invalid trigger/position/precision"}
            )
            return httpx.Response(400 if cls is Binance else 200, json=payload)

        ex = mock_exchange(cls, handle)
        result = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", 105, 97)
        assert result["status"] == "failed" and not result["attached"]
        assert code in result["reason"]
        assert len(calls) == 1
        await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_accepted_stop_survives_take_profit_unknown_without_retry(cls: Any) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 2:
            raise httpx.ReadTimeout("Authorization: Bearer never-persist-me", request=req)
        payload = {"algoId": "stop1", "algoStatus": "NEW"} if cls is Binance else {"orderId": "stop1"}
        return httpx.Response(200, json=success(cls, payload))

    ex = mock_exchange(cls, handle)
    result = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", 105, 97)
    assert result["status"] == "unknown" and not result["attached"]
    assert result["legs"][0]["algo_id"] == "stop1"
    assert result["failed_kind"] == "tp"
    assert "never-persist-me" not in json.dumps(result)
    replay = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", 105, 97, result)
    assert replay == result and len(calls) == 2
    await ex.close()


async def test_bybit_native_partial_body_and_owned_snapshot() -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "POST":
            return httpx.Response(200, json=success(Bybit, {"orderId": "parent", "orderLinkId": "intent1"}))
        return httpx.Response(
            200,
            json=success(
                Bybit,
                {
                    "list": [
                        {
                            "orderId": "stop1",
                            "parentOrderLinkId": "intent1",
                            "orderLinkId": "",
                            "qty": "0.4",
                            "triggerPrice": "97",
                            "stopOrderType": "PartialStopLoss",
                            "reduceOnly": True,
                            "orderStatus": "Untriggered",
                        }
                    ],
                    "nextPageCursor": "",
                },
            ),
        )

    ex = mock_exchange(Bybit, handle)
    await ex.create_order(SYMBOL, "buy", "market", 1, client_order_id="intent1", tp_px=105, sl_px=97)
    body = json.loads(requests[0].content)
    assert {
        k: body[k]
        for k in ("takeProfit", "stopLoss", "tpslMode", "tpTriggerBy", "slTriggerBy", "tpOrderType", "slOrderType")
    } == {
        "takeProfit": "105",
        "stopLoss": "97",
        "tpslMode": "Partial",
        "tpTriggerBy": "LastPrice",
        "slTriggerBy": "LastPrice",
        "tpOrderType": "Market",
        "slOrderType": "Market",
    }
    rows = await ex.attached_algo_orders(SYMBOL)
    assert rows[0]["parent_client_order_id"] == "intent1"
    assert rows[0]["sl_trigger"] == Decimal("97") and rows[0]["quantity"] == Decimal(".4")
    assert requests[1].url.params["orderFilter"] == "StopOrder"
    await ex.close()


@pytest.mark.parametrize("code", ["110092", "110058", "10001"])
async def test_bybit_attachment_error_propagates_before_receipt(code: str) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"retCode": int(code), "retMsg": "invalid trigger/position/precision"})

    ex = mock_exchange(Bybit, handle)
    with pytest.raises(ExchangeError) as error:
        await ex.create_order(SYMBOL, "buy", "market", 1, sl_px=97)
    assert str(error.value.code) == code and len(calls) == 1
    await ex.close()


async def test_bitget_snapshot_paginates_and_cancellation_checks_failure_items() -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["orderIdList"] == [{"orderId": "stop1"}]
            assert body["planType"] == "loss_plan"
            return httpx.Response(
                200,
                json=success(
                    Bitget, {"successList": [], "failureList": [{"orderId": "stop1", "errorMsg": "still active"}]}
                ),
            )
        if "idLessThan" not in req.url.params:
            rows = [
                {
                    "orderId": str(i),
                    "planType": "profit_plan",
                    "triggerPrice": "105",
                    "size": ".4",
                    "clientOid": f"foreign{i}",
                    "planStatus": "live",
                }
                for i in range(100)
            ]
            return httpx.Response(200, json=success(Bitget, {"entrustedList": rows, "endId": "last"}))
        assert req.url.params["idLessThan"] == "last"
        return httpx.Response(
            200,
            json=success(
                Bitget,
                {
                    "entrustedList": [
                        {
                            "orderId": "stop1",
                            "planType": "loss_plan",
                            "triggerPrice": "97",
                            "size": ".4",
                            "clientOid": "ownedstop",
                            "planStatus": "live",
                        }
                    ],
                    "endId": "stop1",
                },
            ),
        )

    ex = mock_exchange(Bitget, handle)
    rows = await ex.attached_algo_orders(SYMBOL)
    assert len(rows) == 101 and rows[-1]["sl_trigger"] == Decimal("97")
    assert rows[-1]["parent_client_order_id"] is None
    result = await ex.cancel_algo_orders(SYMBOL, ["stop1"])
    assert result[0]["status"] == "failed" and result[0]["reason"] == "cancel_item_rejected"
    assert len([r for r in requests if r.method == "POST"]) == 1
    await ex.close()


async def test_binance_snapshot_and_cancel_exact_id_only() -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "algoId": 1,
                        "clientAlgoId": "qslOwned",
                        "orderType": "STOP_MARKET",
                        "quantity": ".4",
                        "triggerPrice": "97",
                        "reduceOnly": True,
                        "algoStatus": "NEW",
                    }
                ],
            )
        assert req.url.params["algoId"] == "1"
        return httpx.Response(200, json={"algoId": 1, "code": "200", "msg": "success"})

    ex = mock_exchange(Binance, handle)
    rows = await ex.attached_algo_orders(SYMBOL)
    assert rows[0]["sl_trigger"] == Decimal("97") and rows[0]["quantity"] == Decimal(".4")
    result = await ex.cancel_algo_orders(SYMBOL, ["1", "1"])
    assert result[0]["status"] == "cancelled" and len(requests) == 2
    assert requests[1].url.path == "/fapi/v1/algoOrder"
    await ex.close()


async def test_bybit_snapshot_follows_cursor() -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        cursor = req.url.params.get("cursor")
        return httpx.Response(200, json=success(Bybit, {"list": [], "nextPageCursor": "next" if not cursor else ""}))

    ex = mock_exchange(Bybit, handle)
    assert await ex.attached_algo_orders(SYMBOL) == []
    assert len(requests) == 2 and requests[1].url.params["cursor"] == "next"
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_short_entry_uses_closing_buy_or_held_sell(cls: Any) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        body = dict(req.url.params) if cls is Binance else json.loads(req.content)
        calls.append(body)
        payload = {"algoId": "1", "algoStatus": "NEW"} if cls is Binance else {"orderId": "1"}
        return httpx.Response(200, json=success(cls, payload))

    ex = mock_exchange(cls, handle)
    await ex.ensure_attached_exits(SYMBOL, "sell", "shortparent", "shortintent", ".4", sl_px=103)
    assert calls[0].get("side", calls[0].get("holdSide")) == ("BUY" if cls is Binance else "sell")
    await ex.close()


@pytest.mark.parametrize("cls,code", [(Binance, "-4116"), (Bitget, "43118")])
async def test_duplicate_child_is_unknown_and_never_resubmitted(cls: Any, code: str) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        payload = {"code": int(code), "msg": "duplicate"} if cls is Binance else {"code": code, "msg": "duplicate"}
        return httpx.Response(400 if cls is Binance else 200, json=payload)

    ex = mock_exchange(cls, handle)
    state = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", sl_px=97)
    assert state["status"] == "unknown"
    await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", sl_px=97, existing=state)
    assert len(calls) == 1
    await ex.close()


async def test_bybit_exact_child_cancellation_failure_preserved() -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"retCode": 110001, "retMsg": "Order does not exist"})

    ex = mock_exchange(Bybit, handle)
    result = await ex.cancel_algo_orders(SYMBOL, ["stop1", "stop1"])
    assert result[0]["status"] == "failed" and len(calls) == 1
    assert calls[0] == {"category": "linear", "symbol": "BTCUSDT", "orderId": "stop1"}
    await ex.close()


@pytest.mark.parametrize("cls,code", [(Binance, "-1007"), (Bitget, "40010")])
async def test_documented_uncertain_code_is_never_retried(cls: Any, code: str) -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        payload = {"code": int(code), "msg": "timeout"} if cls is Binance else {"code": code, "msg": "timeout"}
        return httpx.Response(400 if cls is Binance else 200, json=payload)

    ex = mock_exchange(cls, handle)
    state = await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", sl_px=97)
    assert state["status"] == "unknown"
    await ex.ensure_attached_exits(SYMBOL, "buy", "parent", "intent1", ".4", sl_px=97, existing=state)
    assert len(calls) == 1
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_quantity_floor_uses_real_step_and_preserves_dust(cls: Any) -> None:
    bodies = []

    def handle(req: httpx.Request) -> httpx.Response:
        bodies.append(dict(req.url.params) if cls is Binance else json.loads(req.content))
        return httpx.Response(200, json=success(cls, {"algoId": str(len(bodies)), "orderId": str(len(bodies))}))

    ex = mock_exchange(cls, handle)
    ex._markets["BTCUSDT"].amount_step = 0.005
    state = await ex.ensure_attached_exits(SYMBOL, "buy", "p", "c", Decimal("0.0120000000000000"), sl_px=97.19)
    assert bodies[0].get("quantity", bodies[0].get("size")) == "0.01"
    assert bodies[0]["triggerPrice"] == "97.1"
    assert state["legs"][0]["quantity"] == "0.01"
    dust = await ex.ensure_attached_exits(SYMBOL, "buy", "p", "c", Decimal("0.012"), sl_px=97.19, existing=state)
    assert dust["status"] == "pending_min_qty" and len(bodies) == 1
    grown = await ex.ensure_attached_exits(SYMBOL, "buy", "p", "c", Decimal("0.016"), sl_px=97.19, existing=dust)
    assert len(bodies) == 2 and grown["legs"][-1]["quantity"] == "0.005"
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_minimum_lot_waits_without_post(cls: Any) -> None:
    calls = []
    ex = mock_exchange(cls, lambda r: calls.append(r))
    ex._markets["BTCUSDT"].raw = {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.005", "minQty": "0.01"}]}
    state = await ex.ensure_attached_exits(SYMBOL, "buy", "p", "c", Decimal("0.009"), sl_px=97)
    assert state["status"] == "pending_min_qty" and not state["legs"] and not calls
    await ex.close()


async def test_bybit_attached_quantity_is_plain_and_floored() -> None:
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=success(Bybit, {"orderId": "p"}))

    ex = mock_exchange(Bybit, handle)
    ex._markets["BTCUSDT"].raw = {"lotSizeFilter": {"qtyStep": "0.005", "minOrderQty": "0.01"}}
    await ex.create_order(SYMBOL, "buy", "market", Decimal("0.0120000000000000"), sl_px=97.19)
    assert calls[0]["qty"] == "0.01" and calls[0]["stopLoss"] == "97.1"
    with pytest.raises(InvalidOrderError):
        await ex.create_order(SYMBOL, "buy", "market", Decimal("0.009"), sl_px=97)
    assert len(calls) == 1
    await ex.close()


@pytest.mark.parametrize("code,expected", [(200, "cancelled"), ("200", "cancelled"), ("400", "failed")])
async def test_binance_cancel_documented_code_types(code: Any, expected: str) -> None:
    ex = mock_exchange(Binance, lambda r: httpx.Response(200, json={"algoId": 1, "code": code}))
    assert (await ex.cancel_algo_orders(SYMBOL, ["1"]))[0]["status"] == expected
    await ex.close()


async def test_bybit_missing_parent_uses_persisted_child_ids_not_shared_prefix() -> None:
    page = [
        {"orderId": "child1", "orderLinkId": "", "stopOrderType": "PartialStopLoss"},
        {"orderId": "child2", "orderLinkId": "qsl-exact", "stopOrderType": "PartialStopLoss"},
        {"orderId": "foreign", "orderLinkId": "qsl-exact-extra", "stopOrderType": "PartialStopLoss"},
    ]
    ex = mock_exchange(Bybit, lambda r: httpx.Response(200, json=success(Bybit, {"list": page})))
    persisted = {
        "legs": [
            {"algo_id": "child1", "order_id": "parent-id", "parent_client_order_id": "intent1"},
            {"client_order_id": "qsl-exact", "order_id": "parent-id", "parent_client_order_id": "intent1"},
        ]
    }
    rows = await ex.attached_algo_orders(SYMBOL, existing=persisted)
    assert [r["parent_client_order_id"] for r in rows] == ["intent1", "intent1", None]
    assert rows[0]["order_id"] == "parent-id"
    rows = await ex.attached_algo_orders(SYMBOL)
    assert rows[0]["parent_client_order_id"] == "intent1"
    await ex.close()


async def test_bybit_confirmed_link_survives_field_missing_on_next_snapshot() -> None:
    row = {"orderId": "child", "orderLinkId": "", "parentOrderLinkId": "intent1", "stopOrderType": "PartialStopLoss"}
    ex = mock_exchange(Bybit, lambda r: httpx.Response(200, json=success(Bybit, {"list": [row]})))
    assert (await ex.attached_algo_orders(SYMBOL))[0]["parent_client_order_id"] == "intent1"
    row.pop("parentOrderLinkId")
    assert (await ex.attached_algo_orders(SYMBOL))[0]["parent_client_order_id"] == "intent1"
    await ex.close()


@pytest.mark.parametrize("cls", [Binance, Bitget])
async def test_uncached_lot_rules_are_loaded_before_child_post(cls: Any) -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            market = {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "baseCoin": "BTC",
                "quoteCoin": "USDT",
                "status": "TRADING",
                "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.005", "minQty": "0.01"}],
                "sizeMultiplier": "0.005",
                "minTradeNum": "0.01",
                "pricePlace": "1",
            }
            data = {"symbols": [market]} if cls is Binance else success(cls, [market])
            return httpx.Response(200, json=data)
        body = dict(req.url.params) if cls is Binance else json.loads(req.content)
        assert body.get("quantity", body.get("size")) == "0.01"
        return httpx.Response(200, json=success(cls, {"algoId": "1", "orderId": "1"}))

    ex = mock_exchange(cls, handle)
    ex._markets.clear()
    result = await ex.ensure_attached_exits(SYMBOL, "buy", "p", "c", Decimal("0.0120000000000000"), sl_px=97)
    assert result["legs"][0]["quantity"] == "0.01"
    assert [r.method for r in requests] == ["GET", "POST"]
    await ex.close()

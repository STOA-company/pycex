"""Synthetic transport contracts: no keys, accounts, or exchange sockets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from pycex import OKX, Binance, Bitget
from pycex.exceptions import ExchangeError, HedgeModeNotSupportedError, InvalidOrderError, NotSupportedError
from pycex.models.market import Market
from pycex.models.order import Order

SYMBOL = "BTC/USDT:USDT"
CID = "SyntheticOrder42"
CLASSES = [Binance, Bitget, OKX]


def order_payload(cls, *, ack=False, state=None):
    if cls is Binance:
        return {
            "orderId": "42",
            "clientOrderId": CID,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "2",
            "executedQty": "0" if ack else "1.5",
            "price": "999",
            "avgPrice": "0" if ack else "100",
            "status": state or "PARTIALLY_FILLED",
        }
    if cls is Bitget:
        row = (
            {"orderId": "42", "clientOid": CID}
            if ack
            else {
                "orderId": "42",
                "clientOid": CID,
                "symbol": "BTCUSDT",
                "side": "buy",
                "orderType": "market",
                "size": "2",
                "baseVolume": "1.5",
                "priceAvg": "100",
                "price": "999",
                "state": state or "partially_filled",
            }
        )
        return {"code": "00000", "msg": "success", "data": row}
    row = (
        {"ordId": "42", "clOrdId": CID, "sCode": "0", "sMsg": ""}
        if ack
        else {
            "ordId": "42",
            "clOrdId": CID,
            "instId": "BTC-USDT-SWAP",
            "side": "buy",
            "ordType": "market",
            "sz": "2",
            "accFillSz": "1.5",
            "fillSz": "0.5",
            "avgPx": "100",
            "fillPx": "101",
            "px": "999",
            "state": state or "partially_filled",
        }
    )
    return {"code": "0", "msg": "", "data": [row]}


def exchange(cls, response=None, *, market_type="linear", status=200, pos_mode="one_way_mode"):
    credentials = {} if cls is Binance else {"passphrase": "synthetic-pass"}
    ex = cls(api_key="synthetic-key", secret="synthetic-secret", market_type=market_type, **credentials)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/v2/mix/account/account":
            return httpx.Response(200, json={"code": "00000", "data": {"posMode": pos_mode}})
        if request.url.path == "/api/v5/account/config":
            return httpx.Response(200, json={"code": "0", "data": [{"acctLv": "1"}]})
        payload = response if response is not None else order_payload(cls, ack=request.method == "POST")
        return httpx.Response(status, json=payload)

    ex._http.set_transport_factory(lambda: httpx.MockTransport(handler))
    return ex, seen


def wire(request):
    return json.loads(request.content) if request.content else dict(request.url.params)


def assert_signed(cls, request):
    secret = b"synthetic-secret"
    if cls is Binance:
        unsigned, signature = request.url.query.decode().rsplit("&signature=", 1)
        assert signature == hmac.new(secret, unsigned.encode(), hashlib.sha256).hexdigest()
        assert request.headers["X-MBX-APIKEY"] == "synthetic-key"
    else:
        prefix = "OK-ACCESS-" if cls is OKX else "ACCESS-"
        message = (
            request.headers[prefix + "TIMESTAMP"]
            + request.method
            + request.url.raw_path.decode()
            + request.content.decode()
        )
        expected = base64.b64encode(hmac.new(secret, message.encode(), hashlib.sha256).digest()).decode()
        assert request.headers[prefix + "SIGN"] == expected


@pytest.mark.parametrize(
    "cls,cid_field,reduction,path",
    [
        (Binance, "newClientOrderId", "true", "/fapi/v1/order"),
        (Bitget, "clientOid", "YES", "/api/v2/mix/order/place-order"),
        (OKX, "clOrdId", True, "/api/v5/trade/order"),
    ],
)
async def test_create_wire_fields_and_signature(cls, cid_field, reduction, path):
    ex, seen = exchange(cls)
    try:
        result = await ex.create_order(SYMBOL, "sell", "market", 2, reduce_only=True, client_order_id=CID)
        assert len(seen) == (2 if cls is Bitget else 1)
        request = seen[-1]
        assert request.method == "POST" and request.url.path == path
        body = wire(request)
        assert body[cid_field] == CID
        assert body["reduceOnly"] == reduction
        assert type(body["reduceOnly"]) is type(reduction)
        assert body["side"].lower() == "sell"
        assert "positionSide" not in body and "posSide" not in body and "tradeSide" not in body
        assert_signed(cls, request)
        assert result.client_order_id == CID
        assert result.filled == 0 and result.average is None
        if cls is not Binance:
            assert result.normalized_status == "accepted"
    finally:
        await ex.close()


@pytest.mark.parametrize(
    "cls,lookup,legacy,path",
    [
        (Binance, "origClientOrderId", "orderId", "/fapi/v1/order"),
        (Bitget, "clientOid", "orderId", "/api/v2/mix/order/detail"),
        (OKX, "clOrdId", "ordId", "/api/v5/trade/order"),
    ],
)
async def test_fetch_by_client_id_and_legacy_id(cls, lookup, legacy, path):
    ex, seen = exchange(cls)
    try:
        order = await ex.fetch_order(None, SYMBOL, client_order_id=CID)
        params = dict(seen[-1].url.params)
        assert seen[-1].method == "GET" and seen[-1].url.path == path
        assert params[lookup] == CID and legacy not in params
        assert_signed(cls, seen[-1])
        assert order.client_order_id == CID
        assert order.filled == 1.5 and order.average == 100
        assert order.normalized_status == "partially_filled"
        await ex.fetch_order("42", SYMBOL)
        assert dict(seen[-1].url.params)[legacy] == "42"
        assert lookup not in dict(seen[-1].url.params)
        assert_signed(cls, seen[-1])
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("order_id,client_id", [(None, None), ("42", CID)])
async def test_fetch_requires_one_identifier_without_request(cls, order_id, client_id):
    ex, seen = exchange(cls)
    try:
        with pytest.raises(InvalidOrderError):
            await ex.fetch_order(order_id, SYMBOL, client_order_id=client_id)
        assert seen == []
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
async def test_spot_reduce_only_rejected_without_order(cls):
    ex, seen = exchange(cls, market_type="spot")
    try:
        with pytest.raises(NotSupportedError):
            await ex.create_order("BTC/USDT", "sell", "market", 1, reduce_only=True)
        assert all(r.method != "POST" for r in seen)
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
def test_legacy_positional_and_new_kwargs_sync_twins(cls):
    ex, seen = exchange(cls)
    try:
        ex.create_order_sync(SYMBOL, "buy", "limit", 2, 100)
        body = wire(seen[-1])
        assert body.get("price", body.get("px")) == "100"
        assert "reduceOnly" not in body
        assert not any(k in body for k in ("newClientOrderId", "clientOid", "clOrdId"))
        ex.create_order_sync(SYMBOL, "sell", "market", 2, reduce_only=True, client_order_id=CID)
        ex.fetch_order_sync(None, SYMBOL, client_order_id=CID)
        assert len(seen) == (5 if cls is Bitget else 3)
        assert all(isinstance(r, httpx.Request) for r in seen)
    finally:
        ex.close_sync()


@pytest.mark.parametrize(
    "cls,payload,status",
    [
        (Binance, {"code": -4061, "msg": "Order's position side does not match user's setting."}, 400),
        (Bitget, {"code": "45109", "msg": "The current account is a two-way position", "data": None}, 400),
        (OKX, {"code": "1", "msg": "", "data": [{"sCode": "51000", "sMsg": "Parameter posSide error"}]}, 200),
    ],
)
async def test_hedge_mode_errors_are_typed(cls, payload, status):
    ex, seen = exchange(cls, payload, status=status)
    try:
        with pytest.raises(HedgeModeNotSupportedError):
            await ex.create_order(SYMBOL, "buy", "market", 1)
        assert len(seen) == (2 if cls is Bitget else 1)
    finally:
        await ex.close()


@pytest.mark.parametrize(
    "cls,payload",
    [
        (Binance, {"code": -4062, "msg": "REDUCE_ONLY_CONFLICT"}),
        (
            Bitget,
            {
                "code": "40774",
                "msg": "The order type for unilateral position must also be the unilateral position type.",
                "data": None,
            },
        ),
        (OKX, {"code": "1", "data": [{"sCode": "51000", "sMsg": "Parameter sz error"}]}),
    ],
)
async def test_other_errors_not_mislabeled_hedge(cls, payload):
    ex, _ = exchange(cls, payload, status=400 if cls is Binance else 200)
    try:
        with pytest.raises(ExchangeError) as error:
            await ex.create_order(SYMBOL, "buy", "market", 1)
        assert not isinstance(error.value, HedgeModeNotSupportedError)
    finally:
        await ex.close()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("NEW", "open"),
        ("live", "open"),
        ("PARTIALLY_FILLED", "partially_filled"),
        ("filled", "filled"),
        ("CANCELED", "canceled"),
        ("mmp_canceled", "canceled"),
        ("EXPIRED_IN_MATCH", "expired"),
        ("REJECTED", "rejected"),
        ("", "unknown"),
        ("new_unknown_state", "unknown"),
        ("accepted", "accepted"),
    ],
)
def test_normalized_status_preserves_raw_status(state, expected):
    order = Order(id="42", symbol=SYMBOL, side="buy", type="market", amount=1, status=state)
    assert order.status == state
    assert order.normalized_status == expected


def market(**kwargs):
    return Market(
        symbol=SYMBOL,
        native="BTC-USDT-SWAP",
        base="BTC",
        quote="USDT",
        market_type="linear",
        amount_unit="contract",
        contract_size=0.01,
        min_amount=0.1,
        amount_step=0.1,
        **kwargs,
    )


def test_amount_from_base_exact_decimal_and_base_units():
    assert market().amount_from_base("0.001") == Decimal("0.1")
    assert isinstance(market().amount_from_base("0.001"), Decimal)
    base = market().model_copy(update={"amount_unit": "base", "min_amount": 0.001, "amount_step": 0.001})
    assert base.amount_from_base("0.001") == Decimal("0.001")


@pytest.mark.parametrize("amount", ["0.0001", "0.0015", "0", "-1", "NaN", "Infinity", "not-a-number"])
def test_conversion_never_rounds_or_increases_invalid_amount(amount):
    with pytest.raises(InvalidOrderError):
        market().amount_from_base(amount)


@pytest.mark.parametrize(
    "changes",
    [
        {"market_type": "spot"},
        {"amount_unit": None},
        {"min_amount": None},
        {"amount_step": None},
    ],
)
def test_missing_metadata_never_guessed(changes):
    with pytest.raises(NotSupportedError):
        market().model_copy(update=changes).amount_from_base("0.001")


@pytest.mark.parametrize("changes", [{"contract_size": 0}, {"contract_size": None}, {"amount_step": 0}])
def test_invalid_contract_metadata_rejected(changes):
    with pytest.raises(InvalidOrderError):
        market().model_copy(update=changes).amount_from_base("0.001")


@pytest.mark.parametrize(
    "mode,error_type",
    [("hedge_mode", HedgeModeNotSupportedError), (None, ExchangeError), ("unexpected", ExchangeError)],
)
async def test_bitget_preflight_blocks_unsupported_or_unknown_modes(mode, error_type):
    ex, seen = exchange(Bitget, pos_mode=mode)
    try:
        with pytest.raises(error_type):
            await ex.create_order(SYMBOL, "buy", "market", 1)
        assert len(seen) == 1 and seen[0].method == "GET"
        assert seen[0].url.path == "/api/v2/mix/account/account"
        assert dict(seen[0].url.params) == {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "marginCoin": "USDT"}
        assert_signed(Bitget, seen[0])
    finally:
        await ex.close()


@pytest.mark.parametrize(
    "cls,payload,unit,size,minimum,step",
    [
        (
            Binance,
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.002", "stepSize": "0.002"},
                        ],
                    }
                ]
            },
            "base",
            1.0,
            0.001,
            0.001,
        ),
        (
            Bitget,
            {
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "sizeMultiplier": "0.001",
                        "minTradeNum": "0.002",
                        "symbolStatus": "normal",
                    }
                ],
            },
            "base",
            1.0,
            0.002,
            0.001,
        ),
        (
            OKX,
            {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ctValCcy": "BTC",
                        "settleCcy": "USDT",
                        "ctType": "linear",
                        "ctVal": "0.1",
                        "ctMult": "0.2",
                        "minSz": "0.1",
                        "lotSz": "0.1",
                        "state": "live",
                    }
                ],
            },
            "contract",
            0.02,
            0.1,
            0.1,
        ),
    ],
)
async def test_market_metadata_from_transport(cls, payload, unit, size, minimum, step):
    ex, _ = exchange(cls, payload)
    try:
        markets = await ex.fetch_markets()
        assert len(markets) == 1
        parsed = markets[0]
        assert parsed.amount_unit == unit
        assert parsed.contract_size == size
        assert parsed.min_amount == minimum
        assert parsed.amount_step == step
        if cls is Binance:
            assert parsed.market_min_amount == 0.002
            assert parsed.market_amount_step == 0.002
            for invalid in ("0.001", "0.003"):
                with pytest.raises(InvalidOrderError):
                    parsed.amount_from_base(invalid)
        assert parsed.amount_from_base("0.002") == (Decimal("0.1") if cls is OKX else Decimal("0.002"))
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
async def test_cancelled_partial_fill_retains_average_and_filled(cls):
    ex, _ = exchange(cls, order_payload(cls, state="CANCELED" if cls is Binance else "canceled"))
    try:
        order = await ex.fetch_order("42", SYMBOL)
        assert order.normalized_status == "canceled"
        assert order.filled == 1.5 and order.average == 100
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
async def test_unfilled_average_is_not_limit_price(cls):
    payload = order_payload(cls)
    if cls is Binance:
        payload.update(executedQty="0", avgPrice="0", status="NEW")
    elif cls is Bitget:
        payload["data"].update(baseVolume="0", priceAvg="", state="live")
    else:
        payload["data"][0].update(accFillSz="0", avgPx="", state="live")
    ex, _ = exchange(cls, payload)
    try:
        order = await ex.fetch_order("42", SYMBOL)
        assert order.filled == 0 and order.average is None and order.normalized_status == "open"
    finally:
        await ex.close()


async def test_bitget_reduce_only_ack_without_order_id_remains_queryable():
    ex, seen = exchange(Bitget, {"code": "00000", "data": {"clientOid": CID}})
    try:
        ack = await ex.create_order(SYMBOL, "sell", "market", 1, reduce_only=True, client_order_id=CID)
        assert ack.id == "" and ack.client_order_id == CID
        assert ack.normalized_status == "accepted" and ack.filled == 0 and ack.average is None
        await ex.fetch_order(None, SYMBOL, client_order_id=ack.client_order_id)
        assert seen[-1].url.params["clientOid"] == CID
    finally:
        await ex.close()


def test_decimal_context_cannot_round_subminimum_into_valid_contract():
    precise = market().model_copy(update={"min_amount": 1, "amount_step": 1})
    with pytest.raises(InvalidOrderError):
        precise.amount_from_base("0.009999999999999999999999999999999")


def test_minimum_rejects_a_smaller_but_step_aligned_amount():
    m = market().model_copy(update={"min_amount": 2, "amount_step": 1})
    with pytest.raises(InvalidOrderError):
        m.amount_from_base("0.01")  # one exact contract, minimum is two


def test_market_minimum_rejects_a_smaller_but_step_aligned_amount():
    m = market().model_copy(update={"market_min_amount": 2, "market_amount_step": 1})
    with pytest.raises(InvalidOrderError):
        m.amount_from_base("0.01")


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("payload", [{}, {"orderId": None}])
async def test_empty_success_payload_is_not_an_accepted_order(cls, payload):
    ex, _ = exchange(cls, payload)
    try:
        order = await ex.create_order(SYMBOL, "buy", "market", 1, client_order_id=CID)
        assert order.normalized_status == "unknown"
        assert order.filled == 0 and order.average is None
    finally:
        await ex.close()

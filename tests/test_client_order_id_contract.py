"""client_order_id contract: send on create_order, look up on fetch_order.

Synthetic transport only (no keys, accounts, or exchange sockets). Payload and
signature helpers are taken from ``tests/test_futures_order_safety.py`` on
``feat/futures-order-safety`` (cef4539), minus its reduce_only/perp/posMode parts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from pycex import OKX, Binance, Bitget, Bithumb, Bybit, Korbit
from pycex.exceptions import InvalidOrderError, NotSupportedError

CID = "SyntheticOrder42"
CLASSES = [Binance, Bitget, OKX]
SYMBOLS = {"spot": "BTC/USDT", "linear": "BTC/USDT:USDT"}


def order_payload(cls, *, ack=False):
    if cls is Binance:
        return {
            "orderId": "42",
            "clientOrderId": CID,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "2",
            "executedQty": "0" if ack else "1.5",
            "status": "PARTIALLY_FILLED",
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
                "price": "999",
                "state": "partially_filled",
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
            "px": "999",
            "state": "partially_filled",
        }
    )
    return {"code": "0", "msg": "", "data": [row]}


def exchange(cls, *, market_type="linear"):
    credentials = {} if cls is Binance else {"passphrase": "synthetic-pass"}
    ex = cls(api_key="synthetic-key", secret="synthetic-secret", market_type=market_type, **credentials)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/v5/account/config":
            return httpx.Response(200, json={"code": "0", "data": [{"acctLv": "1"}]})
        return httpx.Response(200, json=order_payload(cls, ack=request.method == "POST"))

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


@pytest.mark.parametrize("market_type", ["spot", "linear"])
@pytest.mark.parametrize(
    "cls,cid_field",
    [(Binance, "newClientOrderId"), (Bitget, "clientOid"), (OKX, "clOrdId")],
)
async def test_create_sends_client_order_id_signed_and_echoes_it(cls, cid_field, market_type):
    ex, seen = exchange(cls, market_type=market_type)
    try:
        result = await ex.create_order(SYMBOLS[market_type], "sell", "market", 2, client_order_id=CID)
        request = seen[-1]  # OKX spot reads account config first
        assert request.method == "POST" and len([r for r in seen if r.method == "POST"]) == 1
        assert wire(request)[cid_field] == CID
        assert_signed(cls, request)
        assert result.client_order_id == CID
    finally:
        await ex.close()


@pytest.mark.parametrize("market_type", ["spot", "linear"])
@pytest.mark.parametrize("cls,cid_field", [(Binance, "newClientOrderId"), (Bitget, "clientOid"), (OKX, "clOrdId")])
async def test_create_without_client_order_id_sends_no_key(cls, cid_field, market_type):
    ex, seen = exchange(cls, market_type=market_type)
    try:
        result = await ex.create_order(SYMBOLS[market_type], "buy", "limit", 2, 100)
        assert cid_field not in wire(seen[-1])
        assert result.raw is not None
    finally:
        await ex.close()


@pytest.mark.parametrize("market_type", ["spot", "linear"])
@pytest.mark.parametrize(
    "cls,lookup,legacy",
    [(Binance, "origClientOrderId", "orderId"), (Bitget, "clientOid", "orderId"), (OKX, "clOrdId", "ordId")],
)
async def test_fetch_by_client_order_id_and_by_legacy_id(cls, lookup, legacy, market_type):
    ex, seen = exchange(cls, market_type=market_type)
    try:
        order = await ex.fetch_order(None, SYMBOLS[market_type], client_order_id=CID)
        params = dict(seen[-1].url.params)
        assert seen[-1].method == "GET"
        assert params[lookup] == CID and legacy not in params
        assert_signed(cls, seen[-1])
        assert order.id == "42"
        # Bitget spot's order parser does not echo clientOid yet (not in the source branch).
        if not (cls is Bitget and market_type == "spot"):
            assert order.client_order_id == CID
        await ex.fetch_order("42", SYMBOLS[market_type])
        params = dict(seen[-1].url.params)
        assert params[legacy] == "42" and lookup not in params
        assert_signed(cls, seen[-1])
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("order_id,client_id", [(None, None), ("42", CID)])
async def test_fetch_requires_exactly_one_identifier_without_request(cls, order_id, client_id):
    ex, seen = exchange(cls)
    try:
        with pytest.raises(InvalidOrderError):
            await ex.fetch_order(order_id, SYMBOLS["linear"], client_order_id=client_id)
        assert seen == []
    finally:
        await ex.close()


@pytest.mark.parametrize("cls", CLASSES)
def test_sync_twins_forward_client_order_id(cls):
    ex, seen = exchange(cls)
    try:
        ex.create_order_sync(SYMBOLS["linear"], "buy", "limit", 2, 100)
        ex.create_order_sync(SYMBOLS["linear"], "sell", "market", 2, client_order_id=CID)
        ex.fetch_order_sync(None, SYMBOLS["linear"], client_order_id=CID)
        assert [r.method for r in seen].count("POST") == 2 and seen[-1].method == "GET"
    finally:
        ex.close_sync()


@pytest.mark.parametrize("cls", [Bithumb, Korbit, Bybit])
async def test_other_exchanges_reject_client_order_id_without_request(cls):
    ex = cls(api_key="k", secret="s")
    seen = []
    ex._http.set_transport_factory(lambda: httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500)))
    symbol = "BTC/USDT" if cls is Bybit else "BTC/KRW"
    try:
        with pytest.raises(NotSupportedError):
            await ex.create_order(symbol, "buy", "market", 1, client_order_id=CID)
        with pytest.raises(NotSupportedError):
            await ex.fetch_order(None, symbol, client_order_id=CID)
        with pytest.raises(NotSupportedError):
            await ex.fetch_order(None, symbol)
        assert seen == []
    finally:
        await ex.close()

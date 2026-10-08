"""Global request options only; mock HTTP is not provider/cross-bot proof.

Expected messages/signatures are independent fixed synthetic vectors. No actual
credential, issued broker code or transport approval is represented here.
"""
from decimal import Decimal

import httpx
import pytest

import pycex.auth as auth
import pycex.exchanges.binance as binance_module
import pycex.exchanges.bitget as bitget_module
import pycex.exchanges.okx as okx_module
from pycex.exceptions import InvalidOrderError, NetworkError

VENUES = ("binance", "bitget", "okx")
MODULES = {"binance": binance_module, "bitget": bitget_module, "okx": okx_module}
CLASSES = {"binance": binance_module.Binance, "bitget": bitget_module.Bitget, "okx": okx_module.OKX}
BROKER_CONSTANTS = {"binance": "BINANCE_BROKER_ID", "bitget": "BITGET_BROKER_ID", "okx": "OKX_BROKER_ID"}
MARKER_HEADERS = {"binance": "X-MBX-BROKER-ID", "bitget": "X-CHANNEL-API-CODE", "okx": "broker-id"}
SIGNATURE_HEADERS = {"bitget": "ACCESS-SIGN", "okx": "OK-ACCESS-SIGN"}
# path, literal content bytes, literal query bytes, fixed synthetic signature.
VECTORS = {
    'bitget_spot': (
        '/api/v2/spot/trade/place-order',
        b'{"symbol": "BTCUSDT", "side": "sell", "orderType": "market", "size": "1.25"}',
        b'',
        'eufgiBNnVb1CzeZjloiqBSizrt0lT2L47O3KXYTMx88=',
    ),
    'bitget_linear': (
        '/api/v2/mix/order/place-order',
        (
            b'{"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed", '
            b'"marginCoin": "USDT", "size": "1.25", "side": "sell", "orderType": "market"}'
        ),
        b'',
        'fJr32w/KT6HraYLbXZMi6iInAyE8e5cmYlgGZyl3YUo=',
    ),
    'okx_spot': (
        '/api/v5/trade/order',
        (
            b'{"instId": "BTC-USDT", "tdMode": "cash", "side": "sell", "ordType": "market", '
            b'"sz": "1.25", "tgtCcy": "base_ccy"}'
        ),
        b'',
        'Km2TpaW+u5vZmXpkld4wmyGvNT/XlmY5kl7cDmYE3vQ=',
    ),
    'okx_linear': (
        '/api/v5/trade/order',
        b'{"instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "sell", "ordType": "market", "sz": "1.25"}',
        b'',
        'subWmqit65GSUEK5EK7VHsNO+vBoh068J4WDlINAJvI=',
    ),
    'bitget_net_true': (
        '/api/v2/mix/order/place-order',
        (
            b'{"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed", '
            b'"marginCoin": "USDT", "size": "1.25", "side": "sell", "orderType": "market", '
            b'"reduceOnly": "YES", "clientOid": "WIRE123"}'
        ),
        b'',
        'hsOZLXJrNG+PUcoFVogcxCIrkZAvBjcwjQGcKzUPp6w=',
    ),
    'bitget_net_false': (
        '/api/v2/mix/order/place-order',
        (
            b'{"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed", '
            b'"marginCoin": "USDT", "size": "1.25", "side": "sell", "orderType": "market", '
            b'"clientOid": "WIRE123"}'
        ),
        b'',
        'CIn8+b3OB7h50WtHB9KPhKJ9USqpjXkG5dkwTSYvbRg=',
    ),
    'okx_net_true': (
        '/api/v5/trade/order',
        (
            b'{"instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "sell", '
            b'"ordType": "market", "sz": "1.25", "posSide": "net", "clOrdId": "WIRE123", '
            b'"reduceOnly": "true"}'
        ),
        b'',
        'KQGs4Us57lNQA1iMgm7TW7KWcVy3v8/vgPO5ouQjCRQ=',
    ),
    'okx_net_false': (
        '/api/v5/trade/order',
        (
            b'{"instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "sell", '
            b'"ordType": "market", "sz": "1.25", "posSide": "net", "clOrdId": "WIRE123"}'
        ),
        b'',
        'yTz4BViv6pzed2DV+Z1cJLADXCaOcejrAxctq3a3z48=',
    ),
    'binance_spot': (
        '/api/v3/order',
        b'',
        (
            b'symbol=BTCUSDT&side=SELL&type=MARKET&quantity=1.25&timestamp=1700000000000&'
            b'signature=2facf889c6feaab9f16ad5d22246daed5bcfae4dbe7dde65accfcfff0dd0ae20'
        ),
        '2facf889c6feaab9f16ad5d22246daed5bcfae4dbe7dde65accfcfff0dd0ae20',
    ),
    'binance_linear': (
        '/fapi/v1/order',
        b'',
        (
            b'symbol=BTCUSDT&side=SELL&type=MARKET&quantity=1.25&timestamp=1700000000000&'
            b'signature=2facf889c6feaab9f16ad5d22246daed5bcfae4dbe7dde65accfcfff0dd0ae20'
        ),
        '2facf889c6feaab9f16ad5d22246daed5bcfae4dbe7dde65accfcfff0dd0ae20',
    ),
    'binance_net_true': (
        '/fapi/v1/order',
        b'',
        (
            b'symbol=BTCUSDT&side=SELL&type=MARKET&quantity=1.25&positionSide=BOTH&'
            b'reduceOnly=true&newClientOrderId=WIRE123&timestamp=1700000000000&'
            b'signature=12b96cb27aa19e3268d174210ba5d20fc2b21af760e3a477f3337c07e820c4d2'
        ),
        '12b96cb27aa19e3268d174210ba5d20fc2b21af760e3a477f3337c07e820c4d2',
    ),
    'binance_net_false': (
        '/fapi/v1/order',
        b'',
        (
            b'symbol=BTCUSDT&side=SELL&type=MARKET&quantity=1.25&positionSide=BOTH&'
            b'newClientOrderId=WIRE123&timestamp=1700000000000&'
            b'signature=d5fa74cb205f604ff1702c94c960754c0f2adc924bed59ee6443ddcffac5ed3a'
        ),
        'd5fa74cb205f604ff1702c94c960754c0f2adc924bed59ee6443ddcffac5ed3a',
    ),
}


@pytest.fixture(autouse=True)
def _fixed_synthetic_clock_and_empty_default_marking(monkeypatch):
    monkeypatch.setattr(auth, "timestamp_ms", lambda: 1700000000000)
    monkeypatch.setattr(auth, "_okx_timestamp", lambda: "2023-11-14T22:13:20.000Z")
    for venue in VENUES:
        monkeypatch.setattr(MODULES[venue], BROKER_CONSTANTS[venue], "")


def _exchange(venue, market_type="linear", **options):
    kwargs = dict(api_key="unit-key", secret="unit-secret", market_type=market_type, **options)
    if venue != "binance":
        kwargs["passphrase"] = "unit-pass"
    exchange = CLASSES[venue](**kwargs)
    if venue == "okx" and market_type == "spot":
        exchange._acct_level = "1"
    return exchange


def _ack(venue, client_id="WIRE123"):
    if venue == "binance":
        return {"orderId": 1,
            "clientOrderId": client_id,
            "status": "NEW",
            "side": "SELL",
            "type": "MARKET",
            "origQty": "1.25",
            "executedQty": "0",
            "price": "0"}
    if venue == "bitget":
        return {"code": "00000", "data": {"orderId": "1", "clientOid": client_id}}
    return {"code": "0", "data": [{"ordId": "1", "clOrdId": client_id, "sCode": "0"}]}


def _assert_vector(request, venue, name):
    path, content, query, signature = VECTORS[name]
    assert request.method == "POST"
    assert request.url.path == path
    assert request.content == content
    assert request.url.query == query
    assert "Authorization" not in request.headers
    if venue == "binance":
        assert request.headers["X-MBX-APIKEY"] == "unit-key"
    else:
        assert request.headers[SIGNATURE_HEADERS[venue]] == signature
        prefix = "OK-ACCESS-" if venue == "okx" else "ACCESS-"
        assert request.headers[prefix + "KEY"] == "unit-key"
        assert request.headers[prefix + "PASSPHRASE"] == "unit-pass"


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("market_type", ("spot", "linear"))
@pytest.mark.parametrize("explicit_defaults", (False, True))
async def test_omitted_and_explicit_defaults_keep_fixed_wire_bytes(httpx_mock, venue, market_type, explicit_defaults):
    httpx_mock.add_response(json=_ack(venue))
    options = {"broker_id": None} if explicit_defaults else {}
    exchange = _exchange(venue, market_type, **options)
    kwargs = {"position_mode": None, "reduce_only": False} if explicit_defaults else {}
    try:
        await exchange.create_order("BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT",
            "sell",
            "market",
            Decimal("1.25"),
            **kwargs)
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        _assert_vector(requests[0], venue, venue + "_" + market_type)
        assert MARKER_HEADERS[venue] not in requests[0].headers
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("reduce_only", (True, False))
async def test_explicit_net_fixed_request_and_signature(httpx_mock, venue, reduce_only):
    httpx_mock.add_response(json=_ack(venue))
    exchange = _exchange(venue)
    try:
        await exchange.create_order("BTC/USDT:USDT",
            "sell",
            "market",
            Decimal("1.25"),
            client_order_id="WIRE123",
            position_mode="net",
            reduce_only=reduce_only)
        request, = httpx_mock.get_requests()
        _assert_vector(request, venue, venue + "_net_" + str(reduce_only).lower())
        assert b"tradeSide" not in request.content
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("mode,market_type", (("hedge", "linear"), ("", "linear"), (False, "linear"), ("net", "spot")))
async def test_unsupported_modes_refused_before_signing_or_spot_lookup(httpx_mock,
    monkeypatch,
    venue,
    mode,
    market_type):
    exchange = _exchange(venue, market_type)

    def forbidden(*args, **kwargs):
        raise AssertionError("sign/config must not be reached")
    monkeypatch.setattr(exchange,
        {"binance": "_signed_params",
        "bitget": "_signed_post",
        "okx": "_auth_headers"}[venue],
        forbidden)
    if venue == "okx":
        monkeypatch.setattr(exchange, "_spot_td_mode", forbidden)
    try:
        with pytest.raises(InvalidOrderError):
            await exchange.create_order("BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT",
                "sell",
                "market",
                1,
                position_mode=mode)
        assert httpx_mock.get_requests() == []
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", ("binance", "bitget"))
@pytest.mark.parametrize("market_type,mode,reduce_only",
    (("linear",
    None,
    True),
    ("spot",
    None,
    True),
    ("linear",
    "net",
    "true"),
    ("linear",
    "net",
    1)))
async def test_new_reduction_requires_explicit_linear_net_boolean(httpx_mock,
    monkeypatch,
    venue,
    market_type,
    mode,
    reduce_only):
    exchange = _exchange(venue, market_type)

    def forbidden(*args, **kwargs):
        raise AssertionError("sign must not be reached")
    monkeypatch.setattr(exchange, "_signed_params" if venue == "binance" else "_signed_post", forbidden)
    try:
        with pytest.raises(InvalidOrderError):
            await exchange.create_order("BTC/USDT", "sell", "market", 1, position_mode=mode, reduce_only=reduce_only)
        assert httpx_mock.get_requests() == []
    finally:
        await exchange.close()


async def test_legacy_okx_reduce_without_mode_keeps_original_bytes(httpx_mock):
    httpx_mock.add_response(json=_ack("okx"))
    exchange = _exchange("okx")
    try:
        await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, reduce_only=True)
        request, = httpx_mock.get_requests()
        assert request.content == (
            b'{"instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "sell", '
            b'"ordType": "market", "sz": "1", "reduceOnly": "true"}'
        )
        assert b"posSide" not in request.content
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue,override,expected",
    (("binance",
    None,
    "Legacy12"),
    ("binance",
    "",
    None),
    ("bitget",
    None,
    "Legacy12"),
    ("bitget",
    "",
    None),
    ("bitget",
    "UnitCode1",
    "UnitCode1"),
    ("okx",
    None,
    "Legacy12"),
    ("okx",
    "",
    None),
    ("okx",
    "UnitCode1",
    "UnitCode1")))
async def test_marking_is_per_instance_and_default_headers_are_preserved(httpx_mock,
    monkeypatch,
    venue,
    override,
    expected):
    monkeypatch.setattr(MODULES[venue], BROKER_CONSTANTS[venue], "Legacy12")
    httpx_mock.add_response(json=_ack(venue))
    exchange = _exchange(venue, broker_id=override)
    try:
        await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1)
        request, = httpx_mock.get_requests()
        assert request.headers.get(MARKER_HEADERS[venue]) == expected
        if venue == "okx":
            import json
            assert json.loads(request.content).get("tag") == expected
        assert getattr(MODULES[venue], BROKER_CONSTANTS[venue]) == "Legacy12"
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue,value",
    (("binance",
    "UnitCode1"),
    ("binance",
    1),
    ("bitget",
    "bad\r\nvalue"),
    ("bitget",
    "bad value"),
    ("bitget",
    "한글"),
    ("bitget",
    1),
    ("okx",
    "bad-tag"),
    ("okx",
    "x" * 17),
    ("okx",
    "bad\n"),
    ("okx",
    1)))
def test_invalid_marking_refused_before_http_client_construction(monkeypatch, venue, value):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP client must not be constructed")
    monkeypatch.setattr(MODULES[venue], "HTTPClient", forbidden)
    with pytest.raises(InvalidOrderError):
        _exchange(venue, broker_id=value)


@pytest.mark.parametrize("prefix", ("", "x" * 37, "한글", "bad\n", 1))
def test_invalid_prefix_refused_before_http_client_construction(monkeypatch, prefix):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP client must not be constructed")
    monkeypatch.setattr(binance_module, "HTTPClient", forbidden)
    with pytest.raises(InvalidOrderError):
        _exchange("binance", client_order_id_prefix=prefix)


@pytest.mark.parametrize("client_id", (None, "Other123", "Prefix" + "x" * 31, "Prefix한글", "Prefix bad", "Prefix\n"))
async def test_final_prefixed_id_is_validated_without_rewrite_or_send(httpx_mock, monkeypatch, client_id):
    exchange = _exchange("binance", client_order_id_prefix="Prefix")

    def forbidden(*args, **kwargs):
        raise AssertionError("sign must not be reached")
    monkeypatch.setattr(exchange, "_signed_params", forbidden)
    try:
        with pytest.raises(InvalidOrderError):
            await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, client_order_id=client_id)
        if client_id:
            with pytest.raises(InvalidOrderError):
                await exchange.fetch_order(None, "BTC/USDT:USDT", client_order_id=client_id)
        assert httpx_mock.get_requests() == []
    finally:
        await exchange.close()


@pytest.mark.parametrize("final_id", ("Prefix123", "Prefix" + "x" * 30))
async def test_same_final_prefixed_id_sent_and_queried_verbatim(httpx_mock, final_id):
    httpx_mock.add_response(method="POST", json=_ack("binance", final_id))
    httpx_mock.add_response(method="GET", json=_ack("binance", final_id))
    exchange = _exchange("binance", client_order_id_prefix="Prefix", broker_id="")
    try:
        first = await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, client_order_id=final_id)
        later = await exchange.fetch_order(None, "BTC/USDT:USDT", client_order_id=final_id)
        post, get = httpx_mock.get_requests()
        assert post.url.params["newClientOrderId"] == get.url.params["origClientOrderId"] == final_id
        assert "PrefixPrefix" not in str(get.url)
        assert first.client_order_id == later.client_order_id == final_id
        assert [r.method for r in (post, get)] == ["POST", "GET"]
    finally:
        await exchange.close()


async def test_bitget_missing_order_id_requires_same_id_query_without_repost(httpx_mock):
    httpx_mock.add_response(method="POST", json={"code": "00000", "data": {"clientOid": "WIRE123"}})
    httpx_mock.add_response(method="GET",
        json={"code": "00000",
        "data": {"orderId": "late1",
        "clientOid": "WIRE123",
        "size": "1",
        "side": "sell",
        "orderType": "market",
        "state": "live"}})
    exchange = _exchange("bitget")
    try:
        pending = await exchange.create_order("BTC/USDT:USDT",
            "sell",
            "market",
            1,
            client_order_id="WIRE123",
            position_mode="net",
            reduce_only=True)
        assert pending.id == "" and pending.client_order_id == "WIRE123"
        later = await exchange.fetch_order(None, "BTC/USDT:USDT", client_order_id="WIRE123")
        post, get = httpx_mock.get_requests()
        assert later.id == "late1"
        assert get.url.params["clientOid"] == "WIRE123"
        assert [r.method for r in (post, get)] == ["POST", "GET"]
        # No conclusion about acceptance/zero orders can be drawn from missing ID.
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", VENUES)
async def test_response_id_mismatch_is_preserved_for_trusted_echo_gate(httpx_mock, venue):
    httpx_mock.add_response(json=_ack(venue, "OTHER123"))
    exchange = _exchange(venue)
    try:
        result = await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, client_order_id="WIRE123")
        assert result.client_order_id == "OTHER123" != "WIRE123"
        assert len(httpx_mock.get_requests()) == 1
        # T must reject/reconcile the mismatch; this adapter is not its ledger.
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", VENUES)
async def test_timeout_does_not_automatically_send_again(httpx_mock, venue):
    httpx_mock.add_exception(httpx.ReadTimeout("synthetic timeout"))
    exchange = _exchange(venue)
    try:
        with pytest.raises(NetworkError):
            await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, client_order_id="WIRE123")
        request, = httpx_mock.get_requests()
        assert request.method == "POST"
    finally:
        await exchange.close()


@pytest.mark.parametrize("venue", ("bitget", "okx"))
async def test_empty_query_remains_unresolved_without_resend(httpx_mock, venue):
    httpx_mock.add_response(method="GET", json={"code": "00000" if venue == "bitget" else "0", "data": []})
    exchange = _exchange(venue)
    try:
        result = await exchange.fetch_order(None, "BTC/USDT:USDT", client_order_id="WIRE123")
        request, = httpx_mock.get_requests()
        assert request.method == "GET"
        assert request.url.params["clientOid" if venue == "bitget" else "clOrdId"] == "WIRE123"
        assert result.id == "" and result.status == ""
        # Empty DTO is not proof of rejection or zero venue orders.
    finally:
        await exchange.close()


async def test_explicit_okx_net_rejects_non_boolean_before_sign(httpx_mock, monkeypatch):
    exchange = _exchange("okx")

    def forbidden(*args, **kwargs):
        raise AssertionError("sign must not be reached")
    monkeypatch.setattr(exchange, "_auth_headers", forbidden)
    try:
        with pytest.raises(InvalidOrderError):
            await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1, position_mode="net", reduce_only="false")
        assert httpx_mock.get_requests() == []
    finally:
        await exchange.close()


async def test_legacy_okx_body_time_constant_lookup_is_preserved(httpx_mock, monkeypatch):
    monkeypatch.setattr(okx_module, "OKX_BROKER_ID", "FirstCode")
    exchange = _exchange("okx")
    monkeypatch.setattr(okx_module, "OKX_BROKER_ID", "LaterCode")
    httpx_mock.add_response(json=_ack("okx"))
    try:
        await exchange.create_order("BTC/USDT:USDT", "sell", "market", 1)
        request, = httpx_mock.get_requests()
        assert request.headers["broker-id"] == "FirstCode"
        assert b'"tag": "LaterCode"' in request.content
    finally:
        await exchange.close()

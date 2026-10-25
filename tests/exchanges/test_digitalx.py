"""Independent document-derived wire server; no production sockets or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from pycex import DigitalX, create_exchange
from pycex.exceptions import (
    AuthenticationError,
    DuplicateOrderError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NetworkError,
    OrderNotFoundError,
    RateLimitError,
)
from pycex.exchanges.digitalx import REJECT_CODES, _map_error


class ContractServer:
    """HTTP transport fake with independent validation and a small exact settlement ledger.

    USDT is a synthetic document-schema pair, not a claim of a listed live market.
    Fills are explicitly driven by tests, never random.
    """

    def __init__(self):
        import secrets

        self.key, self.secret = secrets.token_hex(16), secrets.token_hex(32)
        self.orders, self.trades, self.requests = {}, [], []
        self.free = {
            "krw": Decimal("1000000000"),
            "usdt": Decimal("1000000"),
            "btc": Decimal("10"),
            "xrp": Decimal("10000"),
        }
        self.locked = {k: Decimal(0) for k in self.free}
        self.error = None
        self.quota = None
        self.drop = False
        self.malformed = False

    def reply(self, status=200, data=None, code=None):
        body = (
            {"success": True, "data": data}
            if code is None
            else {"success": False, "error": {"code": status, "message": code}}
        )
        return httpx.Response(status, json=body, headers={"Retry-After": "1"} if status == 429 else {})

    def __call__(self, request):
        raw = request.content.decode() if request.method == "POST" else request.url.query.decode()
        self.requests.append((request.method, request.url.path))
        encoded, signature = raw.rsplit("&signature=", 1)
        supplied = parse_qs("signature=" + signature)["signature"][0]
        if request.headers.get("X-KAPI-KEY") != self.key:
            return self.reply(401, code="UNAUTHORIZED")
        if not hmac.compare_digest(
            supplied, hmac.new(self.secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        ):
            return self.reply(401, code="INVALID_SIGNATURE")
        p = {k: v[0] for k, v in parse_qs(encoded).items()}
        assert p["accountSeq"] == "1"
        assert int(p["timestamp"]) > 0
        if self.quota is not None and sum(m == request.method for m, _ in self.requests) > self.quota:
            return self.reply(429, code="RATE_LIMIT")
        if self.error:
            return self.reply(self.error[0], code=self.error[1])
        if self.drop:
            raise httpx.ReadTimeout("signed request must never leak", request=request)
        if self.malformed:
            return httpx.Response(200, text="not-json")
        path = request.url.path
        if request.method == "POST":
            assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
            assert p["orderType"] in {"limit", "market", "best"}
            if p["orderType"] == "limit":
                assert "price" in p and "qty" in p and "amt" not in p
            else:
                assert "price" not in p
                assert ("amt" in p, "qty" in p) == ((True, False) if p["side"] == "buy" else (False, True))
            if p["orderType"] == "market":
                assert p["timeInForce"] == "ioc"
            if p["orderType"] == "best":
                assert 1 <= int(p["bestNth"]) <= 5
            if p.get("clientOrderId") in {o.get("clientOrderId") for o in self.orders.values()}:
                return self.reply(400, code="DUPLICATE_CLIENT_ORDER_ID")
            oid = str(len(self.orders) + 1)
            order = {**p, "orderId": oid, "status": "open", "filledQty": "0", "filledAmt": "0"}
            self.orders[oid] = order
            base, quote = p["symbol"].split("_")
            reserve = Decimal(
                p.get("amt")
                or (str(Decimal(p["qty"]) * Decimal(p.get("price", "100"))) if p["side"] == "buy" else p["qty"])
            )
            asset = quote if p["side"] == "buy" else base
            order["_reserve"] = reserve
            self.free[asset] -= reserve
            self.locked[asset] += reserve
            return self.reply(data={"orderId": oid})
        if path == "/v2/balance":
            return self.reply(
                data=[
                    {"currency": a, "available": str(v), "tradeInUse": str(self.locked[a]), "withdrawalInUse": "0"}
                    for a, v in self.free.items()
                ]
            )
        if path == "/v2/myTrades":
            return self.reply(data=[t for t in self.trades if t["symbol"] == p["symbol"]])
        if path == "/v2/openOrders":
            return self.reply(
                data=[
                    self.wire(o)
                    for o in self.orders.values()
                    if o["symbol"] == p["symbol"] and o["status"] in {"open", "partiallyFilled"}
                ]
            )
        order = self.orders.get(p.get("orderId")) or next(
            (o for o in self.orders.values() if o.get("clientOrderId") == p.get("clientOrderId")), None
        )
        if order is None:
            return self.reply(404, code="ORDER_NOT_FOUND")
        if request.method == "DELETE":
            self.release(order)
            order["status"] = "partiallyFilledCanceled" if Decimal(order["filledQty"]) else "canceled"
            return self.reply()
        return self.reply(data=self.wire(order))

    @staticmethod
    def wire(order):
        return {k: v for k, v in order.items() if not k.startswith("_")}

    def release(self, order):
        base, quote = order["symbol"].split("_")
        asset = quote if order["side"] == "buy" else base
        self.free[asset] += order["_reserve"]
        self.locked[asset] -= order["_reserve"]
        order["_reserve"] = Decimal(0)

    def fill(self, oid, quantity, terminal=False):
        o = self.orders[oid]
        qty, price = Decimal(quantity), Decimal(o.get("price", "100"))
        base, quote = o["symbol"].split("_")
        asset = quote if o["side"] == "buy" else base
        consumed = qty * price if o["side"] == "buy" else qty
        o["_reserve"] -= consumed
        self.locked[asset] -= consumed
        self.free[base if o["side"] == "buy" else quote] += qty if o["side"] == "buy" else qty * price
        o.update(
            filledQty=str(Decimal(o["filledQty"]) + qty),
            filledAmt=str(Decimal(o["filledAmt"]) + qty * price),
            status="partiallyFilled",
        )
        self.trades.append(
            dict(
                tradeId=len(self.trades) + 1,
                orderId=oid,
                symbol=o["symbol"],
                side=o["side"],
                price=str(price),
                qty=str(qty),
                feeQty="0",
                tradedAt=1,
            )
        )
        if terminal:
            self.release(o)
            o["status"] = "filled"  # Documented IOC: filled can have an unexecuted remainder.


@pytest.fixture
def venue():
    server = ContractServer()
    ex = DigitalX(server.key, server.secret, sandbox=True)
    ex._http.set_transport_factory(lambda: httpx.MockTransport(server))
    return ex, server


KINDS = (
    [("limit", t) for t in ("gtc", "ioc", "fok", "po")]
    + [("market", "ioc")]
    + [("best", t) for t in ("gtc", "ioc", "fok", "po")]
)


@pytest.mark.parametrize("base", ["BTC", "XRP"])
@pytest.mark.parametrize("quote", ["KRW", "USDT"])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind,tif", KINDS)
async def test_market_kind_walk(venue, base, quote, side, kind, tif):
    ex, server = venue
    symbol = base + "/" + quote
    before = await ex.fetch_balance()
    options = {"time_in_force": tif, **({"best_nth": 2} if kind == "best" else {})}
    amount = Decimal("200") if kind != "limit" and side == "buy" else Decimal("2")
    order = await ex.create_order(
        symbol, side, kind, amount, Decimal("100") if kind == "limit" else None, client_order_id="walk:1", **options
    )
    assert order.amount_exact == amount
    assert order.status == "pending"
    assert (await ex.fetch_open_orders(symbol))[0].id == order.id
    count = len(server.requests)
    with pytest.raises(DuplicateOrderError):
        await ex.create_order(
            symbol, side, kind, amount, Decimal("100") if kind == "limit" else None, client_order_id="walk:1", **options
        )
    assert len(server.requests) == count
    # FOK either fills completely or expires: do not invent a partial FOK fill.
    quantity = "2" if tif == "fok" else "0.5"
    server.fill(order.id, quantity, terminal=tif in {"ioc", "fok"})
    state = await ex.fetch_order(None, symbol, client_order_id="walk:1")
    assert state.filled_exact == Decimal(quantity)
    if tif in {"ioc", "fok"}:
        assert state.status == "filled"
    else:
        assert state.status == "partiallyFilled"
        state = await ex.cancel_order(order.id, symbol)
        assert state.status == "partiallyFilledCanceled"
    assert await ex.fetch_open_orders(symbol) == []
    trades = await ex.fetch_my_trades(symbol)
    assert sum(t.amount_exact for t in trades) == state.filled_exact
    after = await ex.fetch_balance()
    sign = 1 if side == "buy" else -1
    assert after.get(base).free_exact == before.get(base).free_exact + sign * Decimal(quantity)
    assert after.get(quote).free_exact == before.get(quote).free_exact - sign * Decimal(quantity) * 100
    assert all(a.locked_exact == 0 for a in after.assets)
    assert await ex.fetch_available_balance_exact(quote) == after.get(quote).free_exact
    assert await ex.fetch_available_balance(quote) == after.get(quote).free
    await ex.close()


async def test_signature_and_decimal_wire(venue):
    ex, server = venue
    qty, price = Decimal("0.123456789123456789"), Decimal("12345.123456789123456789")
    order = await ex.create_order("BTC/KRW", "buy", "limit", qty, price, client_order_id="precision")
    wire = server.orders[order.id]
    assert wire["qty"] == "0.123456789123456789"
    assert wire["price"] == "12345.123456789123456789"
    assert order.amount_exact == qty and order.price_exact == price
    loaded = await ex.fetch_order(order.id, "BTC/KRW")
    assert loaded.amount_exact == qty and loaded.price_exact == price
    ex._secret = "deliberately-wrong-test-value"
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    ex._secret = server.secret
    ex._api_key = "deliberately-wrong-test-value"
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    await ex.close()


@pytest.mark.parametrize(
    "status,code,error",
    [
        (400, "NO_BALANCE", InsufficientBalanceError),
        (200, "NO_BALANCE", InsufficientBalanceError),
        (400, "DUPLICATE_CLIENT_ORDER_ID", DuplicateOrderError),
        (401, "UNAUTHORIZED", AuthenticationError),
        (400, "EXCEED_TIME_WINDOW", AuthenticationError),
        (429, "RATE_LIMIT", RateLimitError),
        (404, "ORDER_NOT_FOUND", OrderNotFoundError),
        (400, "NEW_UNDOCUMENTED_CODE", ExchangeError),
        (503, "NO_BALANCE", ExchangeError),
        *[(400, c, InvalidOrderError) for c in REJECT_CODES],
    ],
)
async def test_errors_no_resend(venue, status, code, error):
    ex, server = venue
    server.error = status, code
    with pytest.raises(error) as caught:
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="error")
    assert type(caught.value) is error
    assert len(server.requests) == 1
    with pytest.raises(DuplicateOrderError):
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="error")
    assert len(server.requests) == 1
    await ex.close()


@pytest.mark.parametrize("mode", ["drop", "malformed"])
async def test_unknown_blocks_replay(venue, mode):
    ex, server = venue
    setattr(server, mode, True)
    with pytest.raises(NetworkError if mode == "drop" else ExchangeError):
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="unknown")
    with pytest.raises(DuplicateOrderError):
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="unknown")
    assert len(server.requests) == 1
    await ex.close()


async def test_concurrent_duplicate(venue):
    ex, server = venue
    results = await asyncio.gather(
        *[ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="concurrent") for _ in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, DuplicateOrderError) for r in results) == 1
    assert len(server.orders) == 1
    await ex.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"order_type": "market", "price": 1},
        {"amount": "NaN"},
        {"amount": "-1"},
        {"price": "Infinity"},
        {"order_type": "best"},
        {"order_type": "market", "time_in_force": "fok"},
        {"time_in_force": "bogus"},
        {"best_nth": 1},
        {"stp": "bogus"},
        {"client_order_id": "x" * 37},
    ],
)
async def test_invalid_input_sends_nothing(venue, kwargs):
    ex, server = venue
    with pytest.raises(InvalidOrderError):
        await ex.create_order(
            **({"symbol": "BTC/KRW", "side": "buy", "order_type": "limit", "amount": 1, "price": 100} | kwargs)
        )
    assert server.requests == []


def test_factory_and_codes():
    assert isinstance(create_exchange("digitalx", sandbox=True), DigitalX)
    assert type(_map_error(200, {"error": {"code": 200, "message": "NO_BALANCE"}})) is InsufficientBalanceError


async def test_ed25519_wire():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(crypto.Encoding.PEM, crypto.PrivateFormat.PKCS8, crypto.NoEncryption()).decode()
    ex = DigitalX("ephemeral-test", pem, sandbox=True, auth_type="ed25519")

    def verify(req):
        raw = req.url.query.decode()
        encoded, sig = raw.rsplit("&signature=", 1)
        key.public_key().verify(base64.b64decode(parse_qs("signature=" + sig)["signature"][0]), encoded.encode())
        return httpx.Response(200, json={"success": True, "data": []})

    ex._http.set_transport_factory(lambda: httpx.MockTransport(verify))
    assert (await ex.fetch_balance()).assets == []
    await ex.close()


async def test_private_quota_is_separate_and_signs_after_wait(venue, monkeypatch):
    ex, server = venue
    from pycex.ratelimit import TokenBucket

    clock = [0.0]
    waits = []

    async def sleep(delay):
        waits.append(delay)
        clock[0] += delay

    bucket = TokenBucket(30, 1, clock=lambda: clock[0], sleep=sleep)
    ex._private_buckets["POST"] = bucket
    for i in range(31):
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id=f"quota-{i}")
    assert len(waits) == 1 and waits[0] == pytest.approx(1 / 30)
    assert ex._private_buckets["DELETE"].limit == 30
    assert ex._private_buckets["GET"].limit == 50
    assert len(server.orders) == 31
    await ex.close()


async def test_aliases_protection_and_no_exponent(venue):
    ex, server = venue
    for kind in ("ioc", "fok"):
        order = await ex.create_order(
            "BTC/KRW",
            "sell",
            kind,
            Decimal("1E-8"),
            Decimal("1E+8"),
            client_order_id=kind,
            stp="cancelBoth",
            price_protection=True,
            price_protection_percent=7,
        )
        p = server.orders[order.id]
        assert (p["orderType"], p["timeInForce"]) == ("limit", kind)
        assert p["qty"] == "0.00000001" and p["price"] == "100000000"
        assert (p["stp"], p["pp"], p["ppPercent"]) == ("cancelBoth", "true", "7")
    await ex.close()


@pytest.mark.parametrize(
    "code", ["KEY_NOT_FOUND", "KEY_EXPIRED", "KEY_DEACTIVATED", "SIGNATURE_NOT_MATCH", "NO_SUBMIT_KEY_OR_SIGNATURE"]
)
def test_official_sandbox_auth_codes(code):
    assert isinstance(_map_error(400, {"error": {"code": 400, "message": code}}), AuthenticationError)


async def test_mock_server_quota_returns_retry_after_without_retry(venue):
    ex, server = venue
    server.quota = 1
    await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="first")
    with pytest.raises(RateLimitError) as caught:
        await ex.create_order("BTC/KRW", "buy", "limit", 1, 100, client_order_id="second")
    assert caught.value.retry_after == 1
    assert len(server.orders) == 1 and len(server.requests) == 2
    await ex.close()

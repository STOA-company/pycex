"""Digital X Open API v2. Contract: tests/fixtures/digitalx/CONTRACT-2026-10-26.md.

The venue was formerly Korbit; public quotation endpoints share that protocol.
No automatic private request retries. Client IDs are reserved before dispatch in
this instance; callers must persist their own idempotency ledger across restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from pycex.auth import timestamp_ms
from pycex.exceptions import (
    AuthenticationError,
    DuplicateOrderError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NetworkError,
    NotSupportedError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
)
from pycex.exchanges.korbit import _CLIENT_ORDER_ID_RE, Korbit, _parse_order
from pycex.http import HTTPClient
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.mytrade import MyTrade
from pycex.models.order import Order
from pycex.ratelimit import ExchangeRateLimiter, TokenBucket
from pycex.symbols import MarketType

# Definitive placement rejections, trading.md#post-_v2_orders. Unknown codes stay unknown.
REJECT_CODES = frozenset(
    {
        "BAD_REQUEST",
        "INVALID_CURRENCY_PAIR",
        "INVALID_USER_STATUS",
        "ONLY_SELL_LIMIT_ORDERS_ALLOWED",
        "ORDER_VALUE_TOO_LARGE",
        "ORDER_VALUE_TOO_SMALL",
        "PRICE_OVER_UPPER_BOUND",
        "PRICE_UNDER_LOWER_BOUND",
        "PRICE_TICK_SIZE_INVALID",
        "TOO_MANY_OPEN_ORDERS",
    }
)
AUTH_CODES = frozenset(
    {
        "EXCEED_TIME_WINDOW",
        "KEY_NOT_FOUND",
        "KEY_EXPIRED",
        "KEY_DEACTIVATED",
        "SIGNATURE_NOT_MATCH",
        "NO_SUBMIT_KEY_OR_SIGNATURE",
    }
)

ORDER_KINDS = {"limit": ("gtc", "ioc", "fok", "po"), "market": ("ioc",), "best": ("gtc", "ioc", "fok", "po")}


def _map_error(status: int, data: dict[str, Any]) -> PyCexError:
    code = (data.get("error") or {}).get("message", "")
    cls: type[ExchangeError] = ExchangeError
    if status >= 500:
        pass  # A proxy/server failure never proves that the order was not received.
    elif status == 429:
        cls = RateLimitError
    elif status in (401, 403) or code in AUTH_CODES:
        return AuthenticationError("Digital X authentication rejected")
    elif code == "DUPLICATE_CLIENT_ORDER_ID":
        cls = DuplicateOrderError
    elif code == "NO_BALANCE":
        cls = InsufficientBalanceError
    elif code in REJECT_CODES:
        cls = InvalidOrderError
    elif code in {"ORDER_NOT_FOUND", "ORDER_ALREADY_CANCELED", "ORDER_ALREADY_EXPIRED", "ORDER_ALREADY_FILLED"}:
        cls = OrderNotFoundError
    # Only expose the symbolic code, never a server echo of the signed request.
    safe_code = code if isinstance(code, str) and code.isupper() and code.replace("_", "").isalnum() else "UNKNOWN"
    return cls(safe_code, code=safe_code, exchange="digitalx")


def _unwrap(data: Any) -> Any:
    if not isinstance(data, dict) or data.get("success") is not True:
        if isinstance(data, dict) and data.get("success") is False:
            try:
                status = int((data.get("error") or {}).get("code", 200))
            except (ValueError, TypeError, AttributeError):
                raise ExchangeError("Malformed error response", exchange="digitalx") from None
            raise _map_error(status, data)
        raise ExchangeError("Malformed response", exchange="digitalx")
    return data.get("data")


def _money(value: Decimal | str | float, field: str) -> str:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise InvalidOrderError(f"{field} must be a decimal", exchange="digitalx") from None
    if not d.is_finite() or d <= 0:
        raise InvalidOrderError(f"{field} must be finite and positive", exchange="digitalx")
    return format(d, "f")


def _order(symbol: str, d: dict[str, Any]) -> Order:
    return _parse_order(symbol, d)


class DigitalX(Korbit):
    """Spot markets from currencyPairs; create_order amount is quote money for market/best buys.

    Result amount/filled are base quantities; amount_quote/cost are quote money. Use *_exact Decimal fields or
    raw string fields for accounting. No keys are read from the environment here.
    ED25519 accepts a PEM private key in ``secret`` and requires the ed25519 extra.
    """

    name = "digitalx"
    order_kinds = ORDER_KINDS
    supports_amend = False
    client_order_id_pattern = r"[0-9a-zA-Z.:_-]{1,36}"

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        timeout: float = 30.0,
        auth_type: str = "hmac",
        account_seq: int = 1,
    ) -> None:
        if market_type != "spot":
            raise NotSupportedError("digitalx is spot only")
        if auth_type not in {"hmac", "ed25519"}:
            raise ValueError("auth_type must be hmac or ed25519")
        if isinstance(account_seq, bool) or not isinstance(account_seq, int) or account_seq < 1:
            raise ValueError("account_seq must be a positive integer")
        self._api_key, self._secret = api_key, secret
        self._auth_type, self.account_seq = auth_type, account_seq
        self._ed_key: Any = None
        if auth_type == "ed25519":
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            key = load_pem_private_key(secret.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("ED25519 private key required")
            self._ed_key = key
        self.market_type, self.sandbox = market_type, sandbox
        self._markets = {}
        # Public protocol is the former Korbit protocol. Private buckets are independent.
        self._rate_limiter = ExchangeRateLimiter("korbit", market_type)
        self._private_buckets = {k: TokenBucket(n, 1) for k, n in {"GET": 50, "POST": 30, "DELETE": 30}.items()}
        self._sent_ids: set[str] = set()
        self._http = HTTPClient(
            "http://127.0.0.1:9999" if sandbox else "https://api.digitalx.miraeasset.com",
            timeout=timeout,
            rate=float("inf"),
            error_mapper=_map_error,
        )

    def _sign(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = {**(params or {}), "accountSeq": self.account_seq, "timestamp": timestamp_ms()}
        encoded = urlencode(p).encode()
        signature = (
            base64.b64encode(self._ed_key.sign(encoded)).decode()
            if self._ed_key is not None
            else hmac.new(self._secret.encode(), encoded, hashlib.sha256).hexdigest()
        )
        return {**p, "signature": signature}

    async def _private(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._private_buckets[method].acquire()
        p = self._sign(params)
        try:
            if method == "POST":
                data = await self._http.post_form(path, data=p, headers=self._headers())
            else:
                call = self._http.get if method == "GET" else self._http.delete
                data = await call(f"{path}?{urlencode(p)}", headers=self._headers())
        except NetworkError:
            raise NetworkError("Digital X transport failure; reconcile before resubmission") from None
        return _unwrap(data)

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float | Decimal | str,
        price: float | Decimal | str | None = None,
        *,
        client_order_id: str | None = None,
        time_in_force: str | None = None,
        best_nth: int | None = None,
        stp: str | None = None,
        price_protection: bool | None = None,
        price_protection_percent: int | None = None,
    ) -> Order:
        side, kind = side.lower(), order_type.lower()
        if kind in {"ioc", "fok"}:
            if time_in_force is not None and time_in_force.lower() != kind:
                raise InvalidOrderError("conflicting time_in_force", exchange=self.name)
            time_in_force, kind = kind, "limit"
        tif = time_in_force.lower() if time_in_force else ("ioc" if kind == "market" else "gtc")
        if side not in {"buy", "sell"} or kind not in ORDER_KINDS or tif not in ORDER_KINDS[kind]:
            raise InvalidOrderError("invalid side/order_type/time_in_force", exchange=self.name)
        if kind == "best" and (time_in_force is None or type(best_nth) is not int or not 1 <= best_nth <= 5):
            raise InvalidOrderError("best requires time_in_force and best_nth 1..5", exchange=self.name)
        if kind != "best" and best_nth is not None:
            raise InvalidOrderError("best_nth requires best order", exchange=self.name)
        p: dict[str, Any] = {"symbol": self.to_native(symbol), "side": side, "orderType": kind, "timeInForce": tif}
        size = _money(amount, "amount")
        if kind == "limit":
            if price is None:
                raise InvalidOrderError("limit requires price", exchange=self.name)
            p.update(qty=size, price=_money(price, "price"))
        else:
            if price is not None:
                raise InvalidOrderError("market/best must omit price", exchange=self.name)
            p["amt" if side == "buy" else "qty"] = size
        if best_nth is not None:
            p["bestNth"] = best_nth
        if stp is not None:
            if stp not in {"none", "cancelTaker", "cancelMaker", "cancelBoth"}:
                raise InvalidOrderError("invalid stp", exchange=self.name)
            p["stp"] = stp
        if price_protection is not None:
            if type(price_protection) is not bool:
                raise InvalidOrderError("price_protection must be bool", exchange=self.name)
            p["pp"] = str(price_protection).lower()
        if price_protection_percent is not None:
            if type(price_protection_percent) is not int or not 1 <= price_protection_percent <= 100:
                raise InvalidOrderError("price_protection_percent must be 1..100", exchange=self.name)
            p["ppPercent"] = str(price_protection_percent)
        if client_order_id is not None:
            if not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
                raise InvalidOrderError("invalid client_order_id", exchange=self.name)
            if client_order_id in self._sent_ids:
                raise DuplicateOrderError(
                    "DUPLICATE_CLIENT_ORDER_ID", code="DUPLICATE_CLIENT_ORDER_ID", exchange=self.name
                )
            p["clientOrderId"] = client_order_id
            self._sent_ids.add(client_order_id)  # before first await, also retained after unknown outcomes
        data = await self._private("POST", "/v2/orders", p)
        if not isinstance(data, dict) or not data.get("orderId"):
            raise ExchangeError("Missing orderId; reconcile before resubmission", exchange=self.name)
        return _order(symbol, {**p, **data, "status": "pending"})

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        await self._private("DELETE", "/v2/orders", {"symbol": self.to_native(symbol), "orderId": order_id})
        # DELETE acknowledges the request, not a completed cancellation.
        return await self.fetch_order(order_id, symbol)

    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order:
        if bool(order_id) == bool(client_order_id):
            raise InvalidOrderError("Provide exactly one of order_id or client_order_id", exchange=self.name)
        key = {"clientOrderId": client_order_id} if client_order_id else {"orderId": order_id}
        data = await self._private("GET", "/v2/orders", {"symbol": self.to_native(symbol), **key})
        if not isinstance(data, dict) or not data.get("orderId"):
            raise ExchangeError("Malformed order response", exchange=self.name)
        return _order(symbol, data)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            raise NotSupportedError("digitalx.fetch_open_orders requires symbol")
        data = await self._private("GET", "/v2/openOrders", {"symbol": self.to_native(symbol), "limit": 1000})
        return [_order(symbol, d) for d in data]

    async def fetch_balance(self) -> Balance:
        data = await self._private("GET", "/v2/balance")
        entries = []
        for d in data:
            free = Decimal(d["available"])
            locked = Decimal(d["tradeInUse"]) + Decimal(d["withdrawalInUse"])
            entries.append(
                BalanceEntry(
                    asset=d["currency"].upper(),
                    free=float(free),
                    locked=float(locked),
                    free_exact=free,
                    locked_exact=locked,
                )
            )
        return Balance(assets=entries, raw=data)

    async def fetch_available_balance_exact(self, asset: str) -> Decimal:
        entry = (await self.fetch_balance()).get(asset)
        return entry.free_exact if entry and entry.free_exact is not None else Decimal(0)

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        if symbol is None:
            raise NotSupportedError("digitalx.fetch_my_trades requires symbol")
        p: dict[str, Any] = {"symbol": self.to_native(symbol), "limit": limit or 500}
        if since is not None:
            p["startTime"] = since
        data = await self._private("GET", "/v2/myTrades", p)
        return [
            MyTrade(
                id=str(d["tradeId"]),
                order_id=str(d["orderId"]),
                symbol=symbol,
                side=d["side"],
                price=float(d["price"]),
                amount=float(d["qty"]),
                fee=float(d.get("feeQty") or 0),
                price_exact=Decimal(d["price"]),
                amount_exact=Decimal(d["qty"]),
                fee_exact=Decimal(d.get("feeQty") or "0"),
                fee_asset=d.get("feeCurrency") or "",
                timestamp=d["tradedAt"],
                raw=d,
            )
            for d in data
        ]

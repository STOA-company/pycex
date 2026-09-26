"""Bithumb spot exchange adapter — Upbit-compatible KRW REST API, v1/v2 mixed.

Bithumb's REST API is a close cousin of Upbit's: the same native symbol
notation (``KRW-BTC``), the same public market-data paths/response shapes,
and JWT (HS256) auth with the same ``access_key``/``nonce``/``query_hash``/
``query_hash_alg`` payload shape — **plus a required ``timestamp`` (ms) field
that Upbit's payload omits** (see :func:`pycex.auth.bithumb_headers`).

Private endpoints are a **v1/v2 mix**, confirmed against
https://apidocs.bithumb.com/reference on 2026-08-30:

- Balance: ``GET /v1/accounts`` (same as Upbit).
- Create order: ``POST /v2/orders`` (field ``order_type``, not ``ord_type``;
  the response only echoes ``order_id``/``market``/``side``/``order_type``/
  ``created_at`` — no price/volume/state).
- Cancel: ``DELETE /v2/order`` (params ``order_id``/``client_order_id``).
- Individual order lookup: ``GET /v1/order?uuid=`` (full detail, including a
  ``trades`` array — same shape as Upbit's order object).
- Open orders: ``GET /v2/orders/pending``.
- Completed orders: ``GET /v2/orders/history`` — **no ``trades`` array**, only
  ``trades_count``.

Because ``/v2/orders/history`` has no per-fill detail, :meth:`fetch_my_trades`
fans out to ``GET /v1/order?uuid=`` once per completed order (``1 + N``
requests, ``N`` capped at 50) to flatten each order's ``trades`` array — see
that method's docstring.

The public-market-data surface is shared with :class:`pycex.exchanges.upbit.Upbit`
via :class:`pycex.exchanges._krw_v1.KrwV1Mixin`.

🚨 **Daily candle boundary differs from Upbit** despite the identical field
shape (``candle_date_time_utc`` is bar-OPEN on both): Upbit's daily candles
reset at 00:00 UTC (09:00 KST); Bithumb's reset at **00:00 KST (= 15:00 UTC
the previous day)**. Confirmed against a live recording — see
``tests/fixtures/NOTES.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pycex.auth import bithumb_headers
from pycex.base import BaseExchange
from pycex.constants import BITHUMB_BASE, CANDLE_VENUES
from pycex.exceptions import InvalidOrderError, NotSupportedError, PyCexError
from pycex.exchanges._krw_v1 import KrwV1Mixin, _parse_balance, _parse_iso_to_ms, _parse_order, map_krw_error
from pycex.exchanges._krw_v1 import _parse_candle as _parse_candle
from pycex.exchanges._krw_v1 import _parse_market as _parse_market
from pycex.exchanges._krw_v1 import _parse_ticker as _parse_ticker
from pycex.http import HTTPClient
from pycex.models.balance import Balance
from pycex.models.market import Market
from pycex.models.mytrade import MyTrade
from pycex.models.order import Order
from pycex.ratelimit import ExchangeRateLimiter
from pycex.symbols import MarketType

# `invalid_jwt` confirmed live 2026-08-30: GET /v1/accounts with a garbage bearer
# token -> HTTP 401 {"error":{"name":"invalid_jwt"}}.
_KST = timezone(timedelta(hours=9))

_AUTH_NAMES = frozenset({"jwt_verification", "invalid_jwt", "expired_jwt", "NotAllowIP"})

# fetch_my_trades fans out 1 GET per completed order to get its trades[] array
# (see docstring below) — cap N to bound that fan-out.
_MY_TRADES_DEFAULT_LIMIT = 20
_MY_TRADES_MAX_LIMIT = 50


class Bithumb(KrwV1Mixin, BaseExchange):
    """Bithumb spot exchange — no sandbox, spot only.

    Daily candles reset at 00:00 KST (15:00 UTC the previous day) — different
    from Upbit, which resets at 00:00 UTC, despite both exchanges returning
    the same ``candle_date_time_utc`` field shape.
    """

    name = "bithumb"
    candle_page_limit = CANDLE_VENUES["bithumb"].page_limit
    # Same `to`-only cursor as Upbit (and the same naive-KST caveat — see `_format_to`).
    candle_paging = CANDLE_VENUES["bithumb"].paging
    _auth_error_names = _AUTH_NAMES
    supported_timeframes = CANDLE_VENUES["bithumb"].timeframes

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        timeout: float = 30.0,
    ) -> None:
        if sandbox:
            raise NotSupportedError("bithumb has no sandbox environment")
        if market_type != "spot":
            raise NotSupportedError("bithumb is spot only")
        self._api_key = api_key
        self._secret = secret
        self.market_type = market_type
        self.sandbox = False
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        # ExchangeRateLimiter is the sole admission gate; keep HTTPClient from delaying after signing.
        self._http = HTTPClient(BITHUMB_BASE, timeout=timeout, rate=float("inf"), error_mapper=_map_error)

    def _headers(self, params: dict[str, Any] | None = None) -> dict[str, str]:
        return bithumb_headers(self._api_key, self._secret, params)

    def _format_to(self, ms: int) -> str:
        """Bithumb's ``to`` is **naive KST** — see :meth:`KrwV1Mixin._format_to`.

        Live probe 2026-08-30: ``to=2026-08-25T00:00:00Z`` and
        ``to=2026-08-25T00:00:00+00:00`` both return HTTP **200** with
        ``{"error":{"name":400,"message":"Invalid parameter. Check the given
        value!"}}``; ``to=2026-08-25T00:00:00`` returns the ``2026-08-23T15:00:00``
        bar as newest — i.e. the value was read as 2026-08-25 00:00 KST
        (= 2026-08-24T15:00Z) and applied exclusively. Upbit reads the same
        field as UTC.
        """
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_KST).strftime("%Y-%m-%dT%H:%M:%S")

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query"):
            data = self._check(await self._http.get("/v1/accounts", headers=self._headers()))
        return _parse_balance(data)

    # ── Trading ──

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        *,
        client_order_id: str | None = None,
    ) -> Order:
        """Place an order via ``POST /v2/orders``.

        Request shape matches Upbit closely (``market``/``side``/``price``/
        ``volume``) but the order-type field is named ``order_type`` (Upbit:
        ``ord_type``), and the response only echoes ``order_id``/``market``/
        ``side``/``order_type``/``created_at`` — not price/volume/state.
        Market buys use ``order_type="price"`` with ``amount`` as the
        **quote-currency total to spend**; market sells use
        ``order_type="market"`` with ``amount`` as the base-asset volume —
        matching every other adapter's convention.

        The returned ``Order``'s ``side``/``type``/``amount``/``price`` echo
        exactly what the caller passed in (``price`` forced to ``None`` for
        market orders) rather than whatever Bithumb's response happens to
        contain — see ``raw`` for the actual response.
        """
        if client_order_id is not None:
            raise NotSupportedError("This adapter does not support client_order_id")
        native = self.to_native(symbol)
        canonical_side = side.lower()
        canonical_type = order_type.lower()
        bithumb_side = "bid" if canonical_side == "buy" else "ask"
        body: dict[str, Any] = {"market": native, "side": bithumb_side}
        if canonical_type == "limit":
            if price is None:
                raise InvalidOrderError("limit order requires a price", exchange="bithumb")
            body["order_type"] = "limit"
            body["volume"] = str(amount)
            body["price"] = str(price)
        elif bithumb_side == "bid":
            body["order_type"] = "price"
            body["price"] = str(amount)
        else:
            body["order_type"] = "market"
            body["volume"] = str(amount)
        async with self._rate_limiter.request("order"):
            data = self._check(await self._http.post("/v2/orders", data=body, headers=self._headers(body)))
        parsed = _parse_order(symbol, _normalize_order_fields(data))
        return parsed.model_copy(
            update={
                "side": canonical_side,
                "type": canonical_type,
                "amount": amount,
                "price": price if canonical_type == "limit" else None,
            }
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        """Cancel via ``DELETE /v2/order``.

        The response is even sparser than create's (``order_id``,
        ``client_order_id``, ``created_at`` — no side/type/price at all).
        ``status`` is forced to ``"cancel"`` — the one fact this call itself
        guarantees — but ``side``/``type`` are deliberately left as ``""``:
        the caller only passes ``order_id``/``symbol`` here, not the original
        side/type, so there is nothing honest to fill them with. Do not guess.
        """
        params = {"order_id": order_id}
        async with self._rate_limiter.request("order"):
            data = self._check(await self._http.delete("/v2/order", params=params, headers=self._headers(params)))
        order = _parse_order(symbol, _normalize_order_fields(data))
        return order.model_copy(update={"status": "cancel"})

    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order:
        if client_order_id is not None or order_id is None:
            raise NotSupportedError("This adapter requires an exchange order ID")
        params = {"uuid": order_id}
        async with self._rate_limiter.request("query"):
            data = self._check(await self._http.get("/v1/order", params=params, headers=self._headers(params)))
        return _parse_order(symbol, data)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"state": "wait"}
        if symbol is not None:
            params["market"] = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = self._check(await self._http.get("/v2/orders/pending", params=params, headers=self._headers(params)))
        orders = data.get("data", [])
        return [_parse_order(self.from_native(o.get("market", "")), _normalize_order_fields(o)) for o in orders]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        """Flatten fills from completed orders.

        Bithumb's v2 "종료 주문" list (``GET /v2/orders/history``) does not carry
        a ``trades`` array — only ``trades_count`` — so this first fetches up to
        ``limit`` (default 20, capped at 50 to bound the fan-out below)
        completed (``state=done``) orders, then makes one
        ``GET /v1/order?uuid=`` call per order to pull its full ``trades[]``
        array (``1 + N`` requests total). Per-trade timestamps come from each
        trade's own ``created_at`` (Bithumb's v1 order detail includes it per
        trade, unlike Upbit's). ``fee`` is always ``0.0`` (not prorated from
        the order's ``paid_fee``) — see ``raw`` for the original trade payload.
        """
        n = min(limit or _MY_TRADES_DEFAULT_LIMIT, _MY_TRADES_MAX_LIMIT)
        params: dict[str, Any] = {"state": "done", "limit": n}
        if symbol is not None:
            params["market"] = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = self._check(await self._http.get("/v2/orders/history", params=params, headers=self._headers(params)))
        trades: list[MyTrade] = []
        for order in data.get("data", []):
            order_id = str(order.get("order_id", ""))
            order_symbol = self.from_native(order.get("market", ""))
            quote = order_symbol.split("/")[1]
            order_side = "buy" if order.get("side") == "bid" else "sell"
            detail_params = {"uuid": order_id}
            async with self._rate_limiter.request("query"):
                detail = self._check(
                    await self._http.get("/v1/order", params=detail_params, headers=self._headers(detail_params))
                )
            for t in detail.get("trades", []):
                created_at = t.get("created_at")
                trades.append(
                    MyTrade(
                        id=str(t.get("uuid", "")),
                        order_id=order_id,
                        symbol=order_symbol,
                        side=order_side,
                        price=float(t.get("price", 0) or 0),
                        amount=float(t.get("volume", 0) or 0),
                        fee=0.0,
                        fee_asset=quote,
                        timestamp=_parse_iso_to_ms(created_at) if created_at else 0,
                        raw=t,
                    )
                )
        if since is not None:
            trades = [t for t in trades if t.timestamp >= since]
        return trades


# ── Errors ──


def _map_error(status: int, data: dict[str, Any]) -> PyCexError | None:
    return map_krw_error(status, data, exchange="bithumb", auth_names=_AUTH_NAMES)


# ── v2 -> v1 field-name normalization ──


def _normalize_order_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Bithumb's v2 endpoints (order creation/cancel/pending/history) use
    ``order_id``/``order_type`` where v1 (and Upbit) use ``uuid``/``ord_type``.
    Map v2 -> v1 field names so the shared ``_parse_order`` parser — which reads
    the v1/Upbit shape — works unchanged for v2 responses too, without a second
    parser implementation."""
    out = dict(d)
    if "uuid" not in out and "order_id" in out:
        out["uuid"] = out["order_id"]
    if "ord_type" not in out and "order_type" in out:
        out["ord_type"] = out["order_type"]
    return out

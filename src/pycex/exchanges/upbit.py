"""Upbit spot exchange adapter — reference implementation for the KRW exchanges.

Upbit has no sandbox/demo environment and is spot-only. Private endpoints are
signed with a JWT (HS256): the payload carries the API access key, a random
nonce, and — when the request has a query (GET/DELETE) or a JSON body that
Upbit treats as a query (POST) — a SHA-512 hash of the urlencoded params as
``query_hash``. See :func:`pycex.auth.upbit_headers`.

The public-market-data surface (symbols, candles, ticker, order book, public
trades) is shared with :class:`pycex.exchanges.bithumb.Bithumb` via
:class:`pycex.exchanges._krw_v1.KrwV1Mixin` — see that module's docstring for
what's shared and why the private/trading methods below are not.
"""

from __future__ import annotations

from typing import Any

from pycex.auth import upbit_headers
from pycex.base import BaseExchange
from pycex.constants import CANDLE_VENUES, UPBIT_BASE
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
_AUTH_NAMES = frozenset(
    {"invalid_query_payload", "jwt_verification", "invalid_jwt", "expired_access_key", "no_authorization_ip"}
)
_RATE_LIMIT_NAMES = frozenset({"too_many_requests"})


class Upbit(KrwV1Mixin, BaseExchange):
    """Upbit spot exchange — no sandbox, spot only."""

    name = "upbit"
    candle_page_limit = CANDLE_VENUES["upbit"].page_limit
    # `to` is the only candle cursor this API has: a page is always the newest
    # `count` bars at or before it. Live probe 2026-08-30, 1d bars, since = now-400d,
    # limit 200 -> the API served the newest slice, not the oldest.
    candle_paging = CANDLE_VENUES["upbit"].paging
    _auth_error_names = _AUTH_NAMES
    _rate_limit_error_names = _RATE_LIMIT_NAMES
    supported_timeframes = CANDLE_VENUES["upbit"].timeframes

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
            raise NotSupportedError("upbit has no sandbox environment")
        if market_type != "spot":
            raise NotSupportedError("upbit is spot only")
        self._api_key = api_key
        self._secret = secret
        self.market_type = market_type
        self.sandbox = False
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        # ExchangeRateLimiter is the sole request gate; keep the legacy HTTP gate inert.
        self._http = HTTPClient(UPBIT_BASE, timeout=timeout, rate=float("inf"), error_mapper=_map_error)

    def _headers(self, params: dict[str, Any] | None = None) -> dict[str, str]:
        return upbit_headers(self._api_key, self._secret, params)

    async def fetch_markets(self) -> list[Market]:
        """Include documented KRW rules without an authenticated request.

        ``public_rules`` alone carries the effective market-buy fill quantum
        in base units, derived from the FAQ's eight-decimal truncation example.
        The generic ``amount_step`` remains unknown (None): the FAQ does not
        establish a general order increment or a separate minimum quantity.
        """
        markets = await super().fetch_markets()
        for market in markets:
            if market.quote == "KRW":
                market.min_notional = 5000.0
                market.public_rules = {
                    "amount_step": "0.00000001",
                    "amount_unit": market.base,
                    "amount_step_scope": "market_buy_fill",
                    "amount_rounding": "truncate",
                    "min_quantity": None,
                    "min_notional": "5000",
                    "notional_unit": "KRW",
                    "amount_step_source": "https://docs.upbit.com/kr/docs/faq-order",
                    "min_notional_source": "https://docs.upbit.com/kr/docs/krw-market-info",
                    "verified_on": "2026-09-20",
                }
        return markets

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query", group="query30"):
            data = self._check(await self._http.get("/v1/accounts", headers=self._headers()))
        return _parse_balance(data)

    # ── Trading ──

    async def create_order(
        self, symbol: str, side: str, order_type: str, amount: float, price: float | None = None
    ) -> Order:
        """Place an order.

        Market buys use Upbit's ``ord_type="price"`` — ``amount`` is then the
        **quote-currency total to spend**, not a base-asset quantity. Market
        sells use ``ord_type="market"`` with ``amount`` as the base-asset
        volume, matching every other adapter's convention.

        The returned ``Order``'s ``side``/``type``/``amount``/``price`` echo
        exactly what the caller passed in (``price`` forced to ``None`` for
        market orders) rather than whatever Upbit's response happens to
        contain — see ``raw`` for the actual response.
        """
        native = self.to_native(symbol)
        canonical_side = side.lower()
        canonical_type = order_type.lower()
        upbit_side = "bid" if canonical_side == "buy" else "ask"
        body: dict[str, Any] = {"market": native, "side": upbit_side}
        if canonical_type == "limit":
            if price is None:
                raise InvalidOrderError("limit order requires a price", exchange="upbit")
            body["ord_type"] = "limit"
            body["volume"] = str(amount)
            body["price"] = str(price)
        elif upbit_side == "bid":
            body["ord_type"] = "price"
            body["price"] = str(amount)
        else:
            body["ord_type"] = "market"
            body["volume"] = str(amount)
        async with self._rate_limiter.request("order"):
            data = self._check(await self._http.post("/v1/orders", data=body, headers=self._headers(body)))
        parsed = _parse_order(symbol, data)
        return parsed.model_copy(
            update={
                "side": canonical_side,
                "type": canonical_type,
                "amount": amount,
                "price": price if canonical_type == "limit" else None,
            }
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        params = {"uuid": order_id}
        async with self._rate_limiter.request("order"):
            data = self._check(await self._http.delete("/v1/order", params=params, headers=self._headers(params)))
        return _parse_order(symbol, data)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        params = {"uuid": order_id}
        async with self._rate_limiter.request("query", group="query30"):
            data = self._check(await self._http.get("/v1/order", params=params, headers=self._headers(params)))
        return _parse_order(symbol, data)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"state": "wait"}
        if symbol is not None:
            params["market"] = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="query30"):
            data = self._check(await self._http.get("/v1/orders", params=params, headers=self._headers(params)))
        return [_parse_order(self.from_native(o.get("market", "")), o) for o in data]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        """Upbit has no dedicated fills endpoint — flatten the ``trades`` array of
        completed (``state=done``) orders instead. Per-trade fees are not prorated
        from the order's ``paid_fee``: ``fee`` is always ``0.0`` (see ``raw`` for the
        original trade payload)."""
        params: dict[str, Any] = {"state": "done", "limit": limit or 100}
        if symbol is not None:
            params["market"] = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="query30"):
            data = self._check(await self._http.get("/v1/orders", params=params, headers=self._headers(params)))
        trades: list[MyTrade] = []
        for order in data:
            order_symbol = self.from_native(order.get("market", ""))
            quote = order_symbol.split("/")[1]
            order_side = "buy" if order.get("side") == "bid" else "sell"
            created_at = order.get("created_at")
            ts = _parse_iso_to_ms(created_at) if created_at else 0
            for t in order.get("trades", []):
                trades.append(
                    MyTrade(
                        id=str(t.get("uuid", "")),
                        order_id=str(order.get("uuid", "")),
                        symbol=order_symbol,
                        side=order_side,
                        price=float(t.get("price", 0) or 0),
                        amount=float(t.get("volume", 0) or 0),
                        fee=0.0,
                        fee_asset=quote,
                        timestamp=ts,
                        raw=t,
                    )
                )
        if since is not None:
            trades = [t for t in trades if t.timestamp >= since]
        return trades


# ── Errors ──


def _map_error(status: int, data: dict[str, Any]) -> PyCexError | None:
    return map_krw_error(status, data, exchange="upbit", auth_names=_AUTH_NAMES, rate_limit_names=_RATE_LIMIT_NAMES)

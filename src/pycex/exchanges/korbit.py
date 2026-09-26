"""Korbit spot exchange adapter (v2 REST — implemented from official docs, no ccxt reference).

Korbit has no sandbox/demo environment and is spot-only. Private endpoints are
authenticated with a single header, ``X-KAPI-KEY``, plus two ordinary request
*parameters* (not headers!) that every private call must carry: ``timestamp``
(Unix ms) and ``signature`` (HMAC-SHA256 hex, see :func:`pycex.auth.korbit_sign`).
For GET/DELETE both travel in the query string; for POST both travel in the
``application/x-www-form-urlencoded`` body. The signature covers the exact
encoded string that will be sent, excluding ``signature`` itself — see
``docs.korbit.co.kr/llms/en/rest_api.md`` (confirmed 2026-08-30).

Every REST response — public or private, success or failure — is wrapped as
``{"success": true/false, "data": ...}`` (confirmed against Task-0 fixtures and
the live docs); :func:`_unwrap` peels that envelope and also raises when
``success`` is ``false`` on an HTTP 200 (Korbit's error envelope does not
require a 4xx status), since :class:`pycex.http.HTTPClient` only consults its
``error_mapper`` for status codes >= 400.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode

from pycex.auth import korbit_sign, timestamp_ms
from pycex.base import BaseExchange
from pycex.constants import CANDLE_VENUES, KORBIT_BASE
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    PyCexError,
    SymbolNotFoundError,
)
from pycex.http import HTTPClient
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.candle import Candle
from pycex.models.market import Market, tick_ladder
from pycex.models.mytrade import MyTrade
from pycex.models.order import Order
from pycex.models.orderbook import OrderBook, OrderBookEntry
from pycex.models.ticker import Ticker
from pycex.models.trade import Trade
from pycex.ratelimit import ExchangeRateLimiter
from pycex.symbols import MarketType, parse_symbol

# Canonical timeframe -> Korbit `interval` value. Confirmed verbatim against
# docs.korbit.co.kr/llms/en/rest_api/quotation.md (2026-08-30): interval values
# are "1","5","15","30","60","240","1D","1W" (minutes for the numeric ones,
# NOT the "1m/5m/.../4h/1D" sidebar shorthand the spec sheet flagged as unverified).
_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "1D"}

_ORDER_NOT_FOUND_MESSAGES = frozenset(
    {"ORDER_NOT_FOUND", "ORDER_ALREADY_CANCELED", "ORDER_ALREADY_EXPIRED", "ORDER_ALREADY_FILLED"}
)

# `clientOrderId`: docs.digitalx.miraeasset.com/llms/en/rest_api/trading.md (POST /v2/orders) — only
# `[0-9a-zA-Z.:_-]{1,36}` is accepted; the same id sent twice is processed once (DUPLICATE_CLIENT_ORDER_ID).
_CLIENT_ORDER_ID_RE = re.compile(r"[0-9a-zA-Z.:_-]{1,36}")

_RULES_SOURCE = "https://docs.digitalx.miraeasset.com/llms/en/rest_api/quotation.md"
_RULES_VERIFIED_ON = "2026-09-26"


class Korbit(BaseExchange):
    """Korbit v2 spot exchange — no sandbox, spot only."""

    name = "korbit"
    candle_page_limit = CANDLE_VENUES["korbit"].page_limit
    # `start` is honoured as a floor but the page is the newest `limit` bars inside
    # [start, end], not the oldest. Live probe 2026-08-30, 1d bars, since = now-400d,
    # limit 200 -> 2026-02-11..2026-08-29 (the newest slice). `end` is the real cursor.
    candle_paging = CANDLE_VENUES["korbit"].paging
    supported_timeframes = CANDLE_VENUES["korbit"].timeframes

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
            raise NotSupportedError("korbit has no sandbox environment")
        if market_type != "spot":
            raise NotSupportedError("korbit is spot only")
        self._api_key = api_key
        self._secret = secret
        self.market_type = market_type
        self.sandbox = False
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        # ExchangeRateLimiter is the sole admission gate; keep HTTPClient from delaying after signing.
        self._http = HTTPClient(KORBIT_BASE, timeout=timeout, rate=float("inf"), error_mapper=_map_error)

    # ── Symbols ──

    def to_native(self, symbol: str) -> str:
        sym = parse_symbol(symbol)
        return f"{sym.base.lower()}_{sym.quote.lower()}"

    def from_native(self, native: str) -> str:
        parts = native.split("_")
        if len(parts) != 2:
            raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")
        base, quote = parts
        return f"{base.upper()}/{quote.upper()}"

    # ── Auth helpers ──

    def _headers(self) -> dict[str, str]:
        return {"X-KAPI-KEY": self._api_key}

    def _sign(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add ``timestamp``/``signature`` to ``params``, signed over the exact encoded
        string that will be sent. Shared by ``_signed_query_path`` (GET/DELETE, where the
        result is urlencoded straight into the request path) and ``_signed_form_body``
        (POST, where the result is handed to :meth:`HTTPClient.post_form` — which encodes
        it the same way), so the signed string and the wire bytes are always identical."""
        p: dict[str, Any] = dict(params or {})
        p["timestamp"] = timestamp_ms()
        message = urlencode(p, doseq=True)
        p["signature"] = korbit_sign(self._secret, message)
        return p

    def _signed_query_path(self, path: str, params: dict[str, Any] | None = None) -> str:
        """Build ``path?query`` with ``timestamp``/``signature`` appended (params=None on
        the actual request call, so httpx sends this string verbatim)."""
        return f"{path}?{urlencode(self._sign(params), doseq=True)}"

    def _signed_form_body(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add ``timestamp``/``signature`` to a POST body dict."""
        return self._sign(params)

    # ── Market Data ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v2/tickers", params={"symbol": native})
        arr = _unwrap(data)
        return _parse_ticker(symbol, arr[0])

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        # `limit` is intentionally unused: Korbit's `GET /v2/orderbook` has no depth
        # param — it takes `level` (a price-grouping step), not a result-count limit
        # (docs.korbit.co.kr/llms/en/rest_api/quotation.md) — so there is nothing to
        # pass it as without changing its semantics.
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v2/orderbook", params={"symbol": native})
        d = _unwrap(data)
        return OrderBook(
            symbol=symbol,
            bids=[OrderBookEntry(price=float(b["price"]), amount=float(b["qty"])) for b in d.get("bids", [])],
            asks=[OrderBookEntry(price=float(a["price"]), amount=float(a["qty"])) for a in d.get("asks", [])],
            timestamp=int(d.get("timestamp", 0) or 0),
            raw=d,
        )

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "symbol": native,
            "interval": _INTERVAL[timeframe],
            "limit": min(limit, self.candle_page_limit),
        }
        if since is not None:
            params["start"] = since
        if until is not None:
            params["end"] = until
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v2/candles", params=params)
        return [_parse_candle(c) for c in _unwrap(data)]

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v2/trades", params={"symbol": native, "limit": limit})
        return [_parse_public_trade(symbol, t) for t in _unwrap(data)]

    async def fetch_markets(self) -> list[Market]:
        """Trading pairs; launched KRW markets also get ``public_rules`` (same keys as Upbit's).

        ``price_tick_ladder`` is read from the public ``GET /v2/tickSizePolicy`` (one call per
        market — the endpoint requires ``symbol``), not hard-coded, so it follows the venue.
        A market whose policy call fails or is malformed simply gets no ladder. Korbit publishes
        no order-quantity step (``amount_step`` stays ``None``), only ``minOrderValue``.
        """
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v2/currencyPairs")
        markets = [_parse_market(m) for m in _unwrap(data)]
        krw = [m for m in markets if m.quote == "KRW" and m.active]
        ladders = await asyncio.gather(*(self._fetch_tick_ladder(m.native) for m in krw))
        for market, ladder in zip(krw, ladders):
            market.public_rules = _krw_rules(market, ladder)
        self._markets = {m.native: m for m in markets}
        return markets

    async def _fetch_tick_ladder(self, native: str) -> list[dict[str, str]] | None:
        try:
            async with self._rate_limiter.request("query"):
                data = await self._http.get("/v2/tickSizePolicy", params={"symbol": native})
            rows = _unwrap(data)
        except PyCexError:
            return None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("symbol") == native:
                tiers = row.get("tickSizePolicy")
                if isinstance(tiers, list):
                    return [
                        {"min_price": str(t.get("priceGte")), "tick": str(t.get("tickSize"))}
                        for t in tiers
                        if isinstance(t, dict)
                    ]
        return None

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query"):
            path = self._signed_query_path("/v2/balance")
            data = await self._http.get(path, headers=self._headers())
        entries = []
        for c in _unwrap(data):
            entries.append(
                BalanceEntry(
                    asset=str(c.get("currency", "")).upper(),
                    free=float(c.get("available", 0) or 0),
                    locked=float(c.get("tradeInUse", 0) or 0) + float(c.get("withdrawalInUse", 0) or 0),
                )
            )
        return Balance(assets=entries, raw=data)

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
        """Place an order.

        Market buys send ``amt`` (quote-currency total to spend); market sells and
        limit orders send ``qty`` (base-asset quantity). The returned ``Order``
        echoes exactly what the caller passed in (``id`` comes from the response,
        which for ``POST /v2/orders`` is only ``{"orderId": ...}`` — see ``raw``).
        """
        if client_order_id is not None and not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
            raise InvalidOrderError("client_order_id must match [0-9a-zA-Z.:_-]{1,36}", exchange="korbit")
        native = self.to_native(symbol)
        canonical_side = side.lower()
        canonical_type = order_type.lower()
        params: dict[str, Any] = {"symbol": native, "side": canonical_side, "orderType": canonical_type}
        if canonical_type == "limit":
            if price is None:
                raise InvalidOrderError("limit order requires a price", exchange="korbit")
            params["price"] = str(price)
            params["qty"] = str(amount)
        elif canonical_side == "buy":
            params["amt"] = str(amount)
        else:
            params["qty"] = str(amount)
        if client_order_id is not None:
            params["clientOrderId"] = client_order_id
        async with self._rate_limiter.request("order"):
            body = self._signed_form_body(params)
            data = await self._http.post_form("/v2/orders", data=body, headers=self._headers())
        result = _unwrap(data)
        order_id = str(result.get("orderId", "")) if isinstance(result, dict) else ""
        return Order(
            id=order_id,
            symbol=symbol,
            side=canonical_side,
            type=canonical_type,
            amount=amount,
            price=price if canonical_type == "limit" else None,
            client_order_id=client_order_id,
            raw=data,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("order"):
            path = self._signed_query_path("/v2/orders", {"symbol": native, "orderId": order_id})
            data = await self._http.delete(path, headers=self._headers())
        # `{"success": true}` only — no order fields to parse; side/type stay unguessed.
        return Order(id=str(order_id), symbol=symbol, side="", type="", amount=0.0, raw=_unwrap(data) or {})

    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order:
        if bool(order_id) == bool(client_order_id):
            raise InvalidOrderError("Provide exactly one of order_id or client_order_id", exchange="korbit")
        native = self.to_native(symbol)
        key = {"clientOrderId": client_order_id} if client_order_id else {"orderId": order_id}
        async with self._rate_limiter.request("query"):
            path = self._signed_query_path("/v2/orders", {"symbol": native, **key})
            data = await self._http.get(path, headers=self._headers())
        return _parse_order(symbol, _unwrap(data))

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            raise NotSupportedError(f"{self.name}.fetch_open_orders requires a symbol")
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            path = self._signed_query_path("/v2/openOrders", {"symbol": native})
            data = await self._http.get(path, headers=self._headers())
        return [_parse_order(symbol, o) for o in _unwrap(data)]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        if symbol is None:
            raise NotSupportedError(f"{self.name}.fetch_my_trades requires a symbol")
        native = self.to_native(symbol)
        params: dict[str, Any] = {"symbol": native, "limit": limit or 100}
        if since is not None:
            params["startTime"] = since
        async with self._rate_limiter.request("query"):
            path = self._signed_query_path("/v2/myTrades", params)
            data = await self._http.get(path, headers=self._headers())
        trades: list[MyTrade] = []
        for t in _unwrap(data):
            trades.append(
                MyTrade(
                    id=str(t.get("tradeId", "")),
                    order_id=str(t.get("orderId", "")),
                    symbol=symbol,
                    side=str(t.get("side", "")).lower(),
                    price=float(t.get("price", 0) or 0),
                    amount=float(t.get("qty", 0) or 0),
                    fee=float(t.get("feeQty", 0) or 0),
                    fee_asset=str(t.get("feeCurrency", "") or ""),
                    timestamp=int(t.get("tradedAt", 0) or 0),
                    raw=t,
                )
            )
        return trades


# ── Parsers ──


def _unwrap(data: Any) -> Any:
    """Peel the ``{"success": true/false, "data": ...}`` envelope.

    Raises the mapped error immediately when ``success`` is ``false`` — Korbit
    can return this with an HTTP 200 status, which ``HTTPClient``'s error_mapper
    (only consulted for status >= 400) would otherwise miss.
    """
    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("error") or {}
        status = err.get("code", 200)
        mapped = _map_error(status, data)
        if mapped is not None:
            raise mapped
        raise ExchangeError(err.get("message", "unknown error"), code=status, exchange="korbit")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _parse_candle(d: dict[str, Any]) -> Candle:
    return Candle(
        timestamp=int(d["timestamp"]),
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
        volume=float(d["volume"]),
    )


def _parse_market(d: dict[str, Any]) -> Market:
    # 🚨 status values are "launched"/"stopped" (confirmed via fixture + live docs
    # fetch 2026-08-30), NOT "active"/"inactive" as an earlier draft of the brief
    # assumed.
    base = str(d.get("baseCurrency", "")).upper()
    quote = str(d.get("quoteCurrency", "")).upper()
    min_notional = d.get("minOrderValue")
    return Market(
        symbol=f"{base}/{quote}",
        native=str(d.get("symbol", "")),
        base=base,
        quote=quote,
        market_type="spot",
        min_notional=float(min_notional) if min_notional not in (None, "") else None,
        active=d.get("status") == "launched",
        raw=d,
    )


def _krw_rules(market: Market, ladder: list[dict[str, str]] | None) -> dict[str, Any]:
    """Document-derived KRW rules, same keys/units as Upbit's ``public_rules`` (values are exact strings)."""
    min_notional = market.raw.get("minOrderValue")
    max_notional = market.raw.get("maxOrderValue")
    rules: dict[str, Any] = {
        "amount_step": None,  # Korbit publishes no order-quantity step
        "min_notional": str(min_notional) if min_notional else None,
        "max_notional": str(max_notional) if max_notional else None,
        "min_quantity": None,
        "amount_unit": market.base,
        "notional_unit": market.quote,
        "min_notional_source": f"{_RULES_SOURCE}#get-_v2_currencyPairs",
        "verified_on": _RULES_VERIFIED_ON,
    }
    if ladder:
        candidate = market.model_copy(update={"public_rules": {"price_tick_ladder": ladder}})
        try:
            tick_ladder(candidate)  # rejects malformed/duplicate tiers
        except ValueError:
            return rules
        rules["price_tick_ladder"] = ladder
        rules["price_tick_ladder_source"] = f"{_RULES_SOURCE}#get-_v2_tickSizePolicy"
        rules["price_tick_ladder_verified_on"] = _RULES_VERIFIED_ON
    return rules


def _parse_ticker(symbol: str, d: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("close", 0) or 0),
        bid=float(d.get("bestBidPrice", 0) or 0),
        ask=float(d.get("bestAskPrice", 0) or 0),
        high=float(d.get("high", 0) or 0),
        low=float(d.get("low", 0) or 0),
        volume=float(d.get("volume", 0) or 0),
        quote_volume=float(d.get("quoteVolume", 0) or 0),
        timestamp=int(d.get("lastTradedAt", 0) or 0),
        raw=d,
    )


def _parse_public_trade(symbol: str, d: dict[str, Any]) -> Trade:
    # docs.korbit.co.kr/llms/en/rest_api/quotation.md: `isBuyerTaker=true` means the
    # taker side of the trade was the buyer, i.e. the trade was a taker BUY.
    return Trade(
        id=str(d.get("tradeId", "")),
        symbol=symbol,
        side="buy" if d.get("isBuyerTaker") else "sell",
        price=float(d.get("price", 0) or 0),
        amount=float(d.get("qty", 0) or 0),
        timestamp=int(d.get("timestamp", 0) or 0),
    )


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    # A market-buy order carries `amt` (quote-currency total spent), not `qty` —
    # `GET /v2/orders` response fields (docs.korbit.co.kr/llms/en/rest_api/trading.md).
    # For that case `amount` below is then the quote total, consistent with
    # `create_order`'s own echo of a market-buy's `amount` argument. Both raw fields
    # are preserved in `raw` regardless of which one was used.
    qty = d.get("qty")
    amt = d.get("amt")
    amount = float(qty) if qty else float(amt or 0)
    price = d.get("price")
    return Order(
        id=str(d.get("orderId", "")),
        client_order_id=d.get("clientOrderId") or None,
        symbol=symbol,
        side=str(d.get("side", "")).lower(),
        type=str(d.get("orderType", "")).lower(),
        amount=amount,
        price=float(price) if price not in (None, "") else None,
        filled=float(d.get("filledQty", 0) or 0),
        status=str(d.get("status", "")),
        timestamp=int(d.get("createdAt", 0) or 0),
        raw=d,
    )


# ── Errors ──


def _map_error(status: int, data: dict[str, Any]) -> PyCexError | None:
    """Map Korbit's ``{"error": {"code": <http>, "message": "<SYMBOLIC>"}}`` body.

    The symbolic code the caller branches on lives in ``error.message``, not
    ``error.code`` (which merely repeats the numeric HTTP status) — confirmed
    both in the spec sheet and the live docs fetch (2026-08-30).
    """
    if status in (401, 403):
        err = data.get("error") or {}
        return AuthenticationError(err.get("message", "unauthorized"))
    err = data.get("error") or {}
    message = err.get("message", "")
    if message == "NO_BALANCE":
        return InsufficientBalanceError(message, code=message, exchange="korbit")
    if message in _ORDER_NOT_FOUND_MESSAGES:
        return OrderNotFoundError(message, code=message, exchange="korbit")
    if message == "EXCEED_TIME_WINDOW":
        return AuthenticationError(message)
    if message == "INVALID_CURRENCY_PAIR":
        return SymbolNotFoundError(message)
    return ExchangeError(message or "unknown error", code=status, exchange="korbit")

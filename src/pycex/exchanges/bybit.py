"""Bybit V5 exchange adapter — spot (``category=spot``) and USDT-margined
linear perpetual futures (``category=linear``, ``market_type="linear"``).

Both market types share the same host/version prefix (``/v5``) and native
symbol shape (``BTCUSDT`` for both spot and linear — Bybit does not append a
contract-type suffix like OKX's ``-SWAP`` or a distinct product family like
Bitget's sandbox ``S``-prefix), so ``from_native`` cannot tell spot and
linear apart from the wire string alone and instead branches on
``self.market_type`` (see that method's docstring).

Doc verification (2026-08-30), via ``WebFetch`` against the live docs:
- ``guide`` (auth): sign string = ``timestamp + api_key + recv_window +
  payload``, where payload is the **query string** for GET and the **raw
  JSON body string** for POST — confirmed verbatim. This is why
  ``create_order``/``cancel_order`` must sign and send the *exact same*
  string (see the ``post_raw`` fix below, mirroring OKX/Bitget): signing
  ``json.dumps(body)`` and then sending via ``HTTPClient.post(data=body)``
  re-serializes the dict with httpx's own (possibly different) JSON
  encoding, breaking the signature the moment separators/key order differ.
- ``market/instrument`` (``GET /v5/market/instruments-info``): confirmed
  field names ``symbol``, ``baseCoin``, ``quoteCoin``, ``status`` (value
  ``"Trading"`` for active) at the top level, and ``priceFilter.tickSize``
  nested one level down. ``lotSizeFilter``'s shape differs by category:
  spot has ``basePrecision``/``minOrderAmt`` (no ``qtyStep``); linear has
  ``qtyStep``/``minNotionalValue`` (no ``basePrecision``) plus a top-level
  ``settleCoin`` — see ``_parse_market``.
- ``order/execution`` (``GET /v5/execution/list``): confirmed field names
  ``execId``, ``orderId``, ``symbol``, ``side``, ``execPrice``, ``execQty``,
  ``execFee``, ``feeCurrency``, ``execTime`` (ms) — all listed as always
  present for a fill row.
- ``error`` (retCode table): confirmed ``110007``/``110004``/``110012``
  (insufficient balance), ``10003``/``10004`` (invalid key / bad signature),
  ``110001``/``170213`` (order does not exist, UTA vs. spot-trade variants),
  ``10006`` (rate limit) — used below in ``_map_error``.

``fetch_positions``/``fetch_funding_rate`` are intentionally left at the
``BaseExchange`` default (``NotSupportedError``) — out of phase-1 scope for
this adapter, matching the brief.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from pycex.auth import bybit_headers
from pycex.base import BaseExchange
from pycex.constants import BYBIT_BASE, BYBIT_REFERRAL_CODE, BYBIT_TESTNET, CANDLE_VENUES, QUOTE_SUFFIXES
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InsufficientBalanceError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.http import HTTPClient
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.candle import Candle
from pycex.models.market import Market, parse_listing_time
from pycex.models.mytrade import MyTrade
from pycex.models.order import Order
from pycex.models.orderbook import OrderBook, OrderBookEntry
from pycex.models.ticker import Ticker
from pycex.models.trade import Trade
from pycex.ratelimit import ExchangeRateLimiter
from pycex.symbols import MarketType, parse_symbol
from pycex.symbols import linear as make_linear_symbol
from pycex.symbols import spot as make_spot_symbol

_TIMEFRAME_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
    "1w": "W",
}


class Bybit(BaseExchange):
    name = "bybit"
    candle_page_limit = CANDLE_VENUES["bybit"].page_limit
    supported_timeframes = CANDLE_VENUES["bybit"].timeframes
    # 🚨 Direction depends on whether `end` is sent. With `start` alone the page is the
    # OLDEST `limit` bars from it; with `start`+`end` (what a range walk sends) Bybit
    # serves the NEWEST `limit` bars inside the window instead, so a `since` cursor
    # never advances past the first page (live probe 2026-08-30: 1h bars over 15 days
    # -> 200 of 360, forward). `end` is the cursor that actually walks the history.
    candle_paging = CANDLE_VENUES["bybit"].paging

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        testnet: bool | None = None,
        timeout: float = 30.0,
        category: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._secret = secret
        self.market_type = market_type
        self.sandbox = self._resolve_sandbox(sandbox, testnet, None)
        self._category = category if category is not None else ("linear" if market_type == "linear" else "spot")
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        base = BYBIT_TESTNET if self.sandbox else BYBIT_BASE
        broker_headers: dict[str, str] = {}
        if BYBIT_REFERRAL_CODE:
            broker_headers["Referer"] = BYBIT_REFERRAL_CODE
        # ExchangeRateLimiter is the sole admission gate.
        self._http = HTTPClient(
            base, timeout=timeout, rate=float("inf"), default_headers=broker_headers, error_mapper=_error_mapper
        )

    def to_native(self, symbol: str) -> str:
        sym = parse_symbol(symbol)
        if sym.settle is not None and sym.settle != sym.quote:
            raise SymbolNotFoundError(
                f"bybit: inverse/cross-settle perpetuals are out of scope (USDT-settled only): {symbol!r}"
            )
        return f"{sym.base}{sym.quote}"

    def from_native(self, native: str) -> str:
        """Bybit's native symbol (``BTCUSDT``) is identical for spot and
        linear — there is no wire-format cue (unlike OKX's ``-SWAP`` suffix)
        to tell them apart, so this branches on ``self.market_type`` instead:
        a linear-configured adapter always resolves to ``BASE/QUOTE:QUOTE``,
        a spot-configured one always to ``BASE/QUOTE`` (never emits a settle
        suffix)."""
        if native in self._markets:
            return self._markets[native].symbol
        for quote in QUOTE_SUFFIXES:
            if native.endswith(quote) and len(native) > len(quote):
                base = native[: -len(quote)]
                if self.market_type == "linear":
                    return make_linear_symbol(base, quote, quote)
                return make_spot_symbol(base, quote)
        raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")

    def _check(self, data: dict[str, Any]) -> dict[str, Any]:
        ret_code = data.get("retCode", 0)
        if ret_code != 0:
            raise _map_error(str(ret_code), str(data.get("retMsg", "Unknown error")))
        result: dict[str, Any] = data.get("result", data)
        return result

    def _auth_get_headers(self, query: str) -> dict[str, str]:
        return bybit_headers(self._api_key, self._secret, query)

    def _signed_get_request(self, path: str, params: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Bake ``params`` into the exact query string once, sign that same
        string, and return ``(path_with_query, headers)``. A prior version
        of this adapter signed a separately-sorted query string while
        letting httpx build the wire query from the original (insertion-
        order) params dict — those two only matched when a request had at
        most one param; ``fetch_my_trades(symbol=..., limit=...)`` produced
        a signed string that didn't match what was actually sent. Building
        the query once and sending the *same* string as the URL (with
        ``params=None`` on the ``HTTPClient.get`` call) makes that class of
        drift structurally impossible — same pattern as Bitget's ``_path``.
        """
        query = urlencode(params)
        full_path = f"{path}?{query}" if query else path
        headers = self._auth_get_headers(query)
        return full_path, headers

    def _auth_post_headers(self, body_str: str) -> dict[str, str]:
        """Sign the exact JSON body string that will go on the wire. Callers
        must pass this same ``body_str`` to ``HTTPClient.post_raw`` — never
        re-serialize the dict via ``post(data=...)``, which would re-encode
        it with httpx's own JSON separators and break the signature."""
        headers = bybit_headers(self._api_key, self._secret, body_str)
        headers["Content-Type"] = "application/json"
        return headers

    # ── Market Data ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        params = {"category": self._category, "symbol": native}
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v5/market/tickers", params=params)
        result = self._check(data)
        return _parse_ticker(symbol, result["list"][0])

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        native = self.to_native(symbol)
        params = {"category": self._category, "symbol": native, "limit": limit}
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v5/market/orderbook", params=params)
        result = self._check(data)
        return _parse_order_book(symbol, result)

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "category": self._category,
            "symbol": native,
            "interval": _TIMEFRAME_MAP.get(timeframe, timeframe),
            "limit": limit,
        }
        if since is not None:
            params["start"] = since
        if until is not None:
            params["end"] = until
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v5/market/kline", params=params)
        result = self._check(data)
        # Bybit serves kline newest-first; the unified contract is ascending.
        return sorted((_parse_candle(k) for k in result.get("list", [])), key=lambda c: c.timestamp)

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        params = {"category": self._category, "symbol": native, "limit": limit}
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v5/market/recent-trade", params=params)
        result = self._check(data)
        return [_parse_trade(symbol, t) for t in result.get("list", [])]

    async def fetch_markets(self) -> list[Market]:
        params = {"category": self._category}
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/v5/market/instruments-info", params=params)
        result = self._check(data)
        markets = [m for d in result.get("list", []) if (m := _parse_market(d, self.market_type)) is not None]
        self._markets = {m.native: m for m in markets}
        return markets

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            data = await self._http.get(full_path, headers=headers)
        result = self._check(data)
        return _parse_balance(result, data)

    # ── Trading ──

    async def create_order(
        self, symbol: str, side: str, order_type: str, amount: float, price: float | None = None
    ) -> Order:
        native = self.to_native(symbol)
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": native,
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Limit" if order_type.lower() == "limit" else "Market",
            "qty": str(amount),
        }
        if price is not None:
            body["price"] = str(price)
            body["timeInForce"] = "GTC"
        async with self._rate_limiter.request("order"):
            body_str = json.dumps(body)
            data = await self._http.post_raw(
                "/v5/order/create", body=body_str, headers=self._auth_post_headers(body_str)
            )
        result = self._check(data)
        return Order(
            id=result.get("orderId", ""),
            symbol=symbol,
            side=side.lower(),
            type=order_type.lower(),
            amount=amount,
            price=price,
            raw=data,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        body = {"category": self._category, "symbol": native, "orderId": order_id}
        async with self._rate_limiter.request("order"):
            body_str = json.dumps(body)
            data = await self._http.post_raw(
                "/v5/order/cancel", body=body_str, headers=self._auth_post_headers(body_str)
            )
        result = self._check(data)
        return Order(id=result.get("orderId", order_id), symbol=symbol, side="", type="", amount=0, raw=data)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        params = {"category": self._category, "symbol": native, "orderId": order_id}
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/order/realtime", params)
            data = await self._http.get(full_path, headers=headers)
        result = self._check(data)
        if result.get("list"):
            return _parse_order(symbol, result["list"][0])
        return Order(id=order_id, symbol=symbol, side="", type="", amount=0)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"category": self._category}
        if symbol:
            params["symbol"] = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/order/realtime", params)
            data = await self._http.get(full_path, headers=headers)
        result = self._check(data)
        return [_parse_order(self.from_native(o.get("symbol", "")), o) for o in result.get("list", [])]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        params: dict[str, Any] = {"category": self._category}
        if symbol is not None:
            params["symbol"] = self.to_native(symbol)
        if limit is not None:
            params["limit"] = limit
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/execution/list", params)
            data = await self._http.get(full_path, headers=headers)
        result = self._check(data)
        trades = [
            _parse_my_trade(symbol if symbol is not None else self.from_native(str(t.get("symbol", ""))), t)
            for t in result.get("list", [])
        ]
        if since is not None:
            trades = [t for t in trades if t.timestamp >= since]
        return trades


# ── Parsers ──


def _parse_ticker(symbol: str, d: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("lastPrice", 0)),
        bid=float(d.get("bid1Price", 0)),
        ask=float(d.get("ask1Price", 0)),
        high=float(d.get("highPrice24h", 0)),
        low=float(d.get("lowPrice24h", 0)),
        volume=float(d.get("volume24h", 0)),
        quote_volume=float(d.get("turnover24h", 0)),
        raw=d,
    )


def _parse_order_book(symbol: str, d: dict[str, Any]) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookEntry(price=float(b[0]), amount=float(b[1])) for b in d.get("b", [])],
        asks=[OrderBookEntry(price=float(a[0]), amount=float(a[1])) for a in d.get("a", [])],
        timestamp=int(d.get("ts", 0)),
        raw=d,
    )


def _parse_candle(k: list[Any]) -> Candle:
    return Candle(
        timestamp=int(k[0]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
    )


def _parse_trade(symbol: str, t: dict[str, Any]) -> Trade:
    return Trade(
        id=t.get("execId", ""),
        symbol=symbol,
        side=t.get("side", "").lower(),
        price=float(t.get("price", 0)),
        amount=float(t.get("size", 0)),
        timestamp=int(t.get("time", 0)),
    )


def _parse_market(d: dict[str, Any], market_type: MarketType) -> Market | None:
    """Parse one ``GET /v5/market/instruments-info`` row (see module
    docstring for field-name confirmation).

    ``lotSizeFilter``'s shape differs by category (confirmed via WebFetch,
    2026-08-30): spot carries ``basePrecision`` (already a step size, not a
    decimal-place count) and no ``qtyStep``; linear carries ``qtyStep`` and
    no ``basePrecision``. Read ``qtyStep`` first and fall back to
    ``basePrecision`` so one line covers both. The notional-floor field name
    also differs: spot's ``minOrderAmt`` vs. linear's ``minNotionalValue``.

    Linear rows additionally carry a top-level ``settleCoin``; only
    USDT-settled contracts are in scope for this phase (matching
    ``to_native``'s rejection of inverse/cross-settle symbols), so a linear
    row whose settle currency isn't the quote currency is skipped (returns
    ``None``) rather than silently mislabeled as USDT-settled.
    """
    native = str(d.get("symbol", ""))
    base = str(d.get("baseCoin", ""))
    quote = str(d.get("quoteCoin", ""))
    lot_filter = d.get("lotSizeFilter") or {}
    if market_type == "linear":
        settle = str(d.get("settleCoin") or quote)
        if settle != quote:
            return None
        symbol = make_linear_symbol(base, quote, quote)
        min_notional_raw = lot_filter.get("minNotionalValue")
    else:
        symbol = make_spot_symbol(base, quote)
        min_notional_raw = lot_filter.get("minOrderAmt")
    price_filter = d.get("priceFilter") or {}
    tick = price_filter.get("tickSize")
    step = lot_filter.get("qtyStep") or lot_filter.get("basePrecision")
    return Market(
        symbol=symbol,
        native=native,
        base=base,
        quote=quote,
        market_type=market_type,
        price_tick=float(tick) if tick not in (None, "") else None,
        amount_step=float(step) if step not in (None, "") else None,
        min_notional=float(min_notional_raw) if min_notional_raw not in (None, "") else None,
        active=d.get("status") == "Trading",
        listed_at=parse_listing_time(d.get("launchTime")),
        raw=d,
    )


def _parse_balance(result: dict[str, Any], raw: dict[str, Any]) -> Balance:
    entries = []
    for account in result.get("list", []):
        for coin in account.get("coin", []):
            free = float(coin.get("availableToWithdraw", 0))
            locked = float(coin.get("locked", 0))
            if free > 0 or locked > 0:
                entries.append(BalanceEntry(asset=coin["coin"], free=free, locked=locked))
    return Balance(assets=entries, raw=raw)


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    return Order(
        id=d.get("orderId", ""),
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("orderType", "").lower(),
        amount=float(d.get("qty", 0)),
        price=float(d["price"]) if d.get("price") and float(d["price"]) > 0 else None,
        filled=float(d.get("cumExecQty", 0)),
        status=d.get("orderStatus", ""),
        timestamp=int(d.get("createdTime", 0)),
        raw=d,
    )


def _parse_my_trade(symbol: str, t: dict[str, Any]) -> MyTrade:
    return MyTrade(
        id=str(t.get("execId", "")),
        order_id=str(t.get("orderId", "")),
        symbol=symbol,
        side=str(t.get("side", "")).lower(),
        price=float(t.get("execPrice", 0) or 0),
        amount=float(t.get("execQty", 0) or 0),
        fee=float(t.get("execFee", 0) or 0),
        fee_asset=str(t.get("feeCurrency", "") or ""),
        timestamp=int(t.get("execTime", 0) or 0),
        raw=t,
    )


# ── Errors ──

# Confirmed via https://bybit-exchange.github.io/docs/v5/error (see module docstring).
_INSUFFICIENT_BALANCE_CODES = frozenset({"110007", "110004", "110012"})
_AUTH_CODES = frozenset({"10003", "10004"})
_ORDER_NOT_FOUND_CODES = frozenset({"110001", "170213"})
_RATE_LIMIT_CODES = frozenset({"10006"})


def _map_error(code: str, msg: str) -> PyCexError:
    if code in _INSUFFICIENT_BALANCE_CODES:
        return InsufficientBalanceError(msg, code=code, exchange="bybit")
    if code in _AUTH_CODES:
        return AuthenticationError(msg)
    if code in _ORDER_NOT_FOUND_CODES:
        return OrderNotFoundError(msg, code=code, exchange="bybit")
    if code in _RATE_LIMIT_CODES:
        return RateLimitError(msg, code=code, exchange="bybit")
    return ExchangeError(msg, code=code, exchange="bybit")


def _error_mapper(status: int, data: dict[str, Any]) -> PyCexError | None:
    code = data.get("retCode")
    if code is None or str(code) == "0":
        return None
    return _map_error(str(code), str(data.get("retMsg", "Unknown error")))

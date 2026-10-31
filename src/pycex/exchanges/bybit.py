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

``fetch_funding_rate`` is left at the ``BaseExchange`` default
(``NotSupportedError``). Linear ``fetch_positions``/``fetch_leverage``/
``set_leverage``/``set_margin_mode``/``fetch_account_config`` and
``create_order(reduce_only=)`` are the isolated-perp surface (docs: ``position/set-leverage``
110043 = already that value, ``account/set-margin-mode``, ``position/switch-isolated``
110026 = already in mode, ``position/list``, ``account/info``; retCode ``10006`` =
rate limit, ``10010`` = IP not in the key's allow-list, ``110072`` = duplicate orderLinkId).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from pycex.auth import bybit_headers
from pycex.base import BaseExchange
from pycex.constants import BYBIT_BASE, BYBIT_REFERRAL_CODE, BYBIT_TESTNET, CANDLE_VENUES, QUOTE_SUFFIXES
from pycex.exceptions import (
    AuthenticationError,
    DuplicateOrderError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.http import HTTPClient
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.candle import Candle
from pycex.models.market import Market, parse_listing_time, select_markets
from pycex.models.mytrade import MyTrade
from pycex.models.order import Order
from pycex.models.orderbook import OrderBook, OrderBookEntry
from pycex.models.position import Position
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

_CLIENT_ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")


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
        return _parse_ticker(symbol, result["list"][0], timestamp=int(data.get("time", 0) or 0))

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

    async def fetch_markets(self, *, symbols: Sequence[str] | None = None) -> list[Market]:
        # PR #8: collect all instrument pages so cached trading rules cover the venue.
        markets: list[Market] = []
        cursor = ""
        seen_cursors: set[str] = set()
        page_count = 0
        while True:
            params: dict[str, Any] = {"category": self._category}
            # Bybit rejects limit/cursor for spot; only derivatives paginate.
            if self._category != "spot":
                params["limit"] = 1000
                if cursor:
                    params["cursor"] = cursor
            async with self._rate_limiter.request("query"):
                data = await self._http.get("/v5/market/instruments-info", params=params)
            result = self._check(data)
            page_count += 1
            markets.extend(m for d in result.get("list", []) if (m := _parse_market(d, self.market_type)) is not None)
            next_cursor = str(result.get("nextPageCursor") or "")
            if self._category == "spot" or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise ExchangeError("bybit: instruments cursor repeated", exchange="bybit")
            if page_count >= 20:
                raise ExchangeError("bybit: instruments page limit exceeded", exchange="bybit")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        self._markets = {m.native: m for m in markets}
        return select_markets(markets, symbols)

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            data = await self._http.get(full_path, headers=headers)
        result = self._check(data)
        return _parse_balance(result, data)

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
        market_unit: str | None = None,
        reduce_only: bool = False,
    ) -> Order:
        """``reduce_only`` (linear, one-way position mode) sends ``reduceOnly=true``
        (POST /v5/order/create, https://bybit-exchange.github.io/docs/v5/order/create-order);
        spot has no position to reduce, so it is refused. ``client_order_id`` is ``orderLinkId``
        (unique per account: a repeat is rejected, retCode 110072 -> ``DuplicateOrderError``)."""
        if client_order_id is not None:
            _validated_client_order_id(client_order_id)
        if reduce_only and self.market_type != "linear":
            raise InvalidOrderError(
                "bybit: reduce_only applies to linear perpetuals (market_type='linear') only",
                code="reduceOnly",
                exchange="bybit",
            )
        native = self.to_native(symbol)
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": native,
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Limit" if order_type.lower() == "limit" else "Market",
            "qty": str(amount),
        }
        # https://bybit-exchange.github.io/docs/v5/order/create-order
        is_spot_market = self.market_type == "spot" and body["orderType"] == "Market"
        if market_unit is not None and (not is_spot_market or market_unit not in ("baseCoin", "quoteCoin")):
            raise InvalidOrderError(
                "bybit: market_unit requires a spot market order and baseCoin/quoteCoin", exchange="bybit"
            )
        if is_spot_market:
            body["marketUnit"] = market_unit or ("quoteCoin" if side.lower() == "buy" else "baseCoin")
        if price is not None:
            body["price"] = str(price)
            body["timeInForce"] = "GTC"
        if reduce_only:
            body["reduceOnly"] = True
        if client_order_id is not None:
            body["orderLinkId"] = client_order_id
        async with self._rate_limiter.request("order"):
            body_str = json.dumps(body)
            data = await self._http.post_raw(
                "/v5/order/create", body=body_str, headers=self._auth_post_headers(body_str)
            )
        result = self._check(data)
        quote_amount = self.market_type == "spot" and body.get("marketUnit") == "quoteCoin"
        return Order(
            id=result.get("orderId", ""),
            client_order_id=result.get("orderLinkId") or client_order_id or None,
            symbol=symbol,
            side=side.lower(),
            type=order_type.lower(),
            amount=0.0 if quote_amount else amount,
            amount_exact=None if quote_amount or self.market_type != "spot" else Decimal(str(amount)),
            amount_quote=Decimal(str(amount)) if quote_amount else None,
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

    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order:
        if bool(order_id) == bool(client_order_id):
            raise InvalidOrderError("Provide exactly one of order_id or client_order_id", exchange="bybit")
        if client_order_id is not None:
            _validated_client_order_id(client_order_id)
        native = self.to_native(symbol)
        lookup = {"orderLinkId": client_order_id} if client_order_id else {"orderId": order_id}
        params = {"category": self._category, "symbol": native, **lookup}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            async with self._rate_limiter.request("query", group="private"):
                full_path, headers = self._signed_get_request(path, params)
                try:
                    data = await self._http.get(full_path, headers=headers)
                    result = self._check(data)
                except OrderNotFoundError:
                    if path.endswith("/history"):
                        raise
                    continue
            if result.get("list"):
                return _parse_order(symbol, result["list"][0], self.market_type)
        raise OrderNotFoundError("bybit: order not found", exchange="bybit")

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        """``GET /v5/order/realtime``. Without ``symbol`` on a linear market: **all** open USDT-settled orders of the
        account (``settleCoin=USDT``, all pages). A failed or malformed page raises — it never means "no orders"."""
        params: dict[str, Any] = {"category": self._category}
        if symbol:
            params["symbol"] = self.to_native(symbol)
        elif self.market_type == "linear":
            params["settleCoin"] = "USDT"
            params["limit"] = 50
        rows: list[dict[str, Any]] = []
        for _ in range(50):
            async with self._rate_limiter.request("query", group="private"):
                full_path, headers = self._signed_get_request("/v5/order/realtime", params)
                data = await self._http.get(full_path, headers=headers)
            result = self._check(data)
            page = result.get("list", []) if isinstance(result, dict) else None
            if not isinstance(page, list) or not all(isinstance(o, dict) for o in page):
                raise ExchangeError("bybit: incomplete open-order snapshot", exchange="bybit")
            rows.extend(page)
            cursor = result.get("nextPageCursor")
            if not cursor:
                return [_parse_order(self.from_native(o.get("symbol", "")), o, self.market_type) for o in rows]
            params["cursor"] = cursor
        raise ExchangeError("bybit: open-order snapshot did not terminate", exchange="bybit")

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

    # ── Perpetual account settings (USDT linear) ──

    async def _account_info(self) -> dict[str, Any]:
        """``GET /v5/account/info`` — ``marginMode`` (``ISOLATED_MARGIN``/``REGULAR_MARGIN``/``PORTFOLIO_MARGIN``)
        and ``unifiedMarginStatus`` (1 = classic, retired and refused; 3/4/5 = unified). Bybit's unified account keeps
        the margin mode per **account** (not per symbol); a classic account keeps it per symbol."""
        async with self._rate_limiter.request("query", group="private"):
            full_path, headers = self._signed_get_request("/v5/account/info", {})
            data = await self._http.get(full_path, headers=headers)
        info = self._check(data)
        if not isinstance(info, dict) or "marginMode" not in info:
            raise ExchangeError("bybit: incomplete account info", exchange="bybit")
        return info

    async def fetch_account_config(self) -> dict[str, Any]:
        """Account info plus ``mgnMode`` (``isolated``/``cross``/``portfolio``, ``None`` if unrecognised) normalised
        from the unified account's ``marginMode``. Classic accounts (``unifiedMarginStatus`` 1) were retired by Bybit
        (2025-11, forced upgrade to UTA): they are refused rather than guessed at."""
        info = dict(await self._account_info())
        if str(info.get("unifiedMarginStatus")) == "1":
            raise NotSupportedError("bybit: classic accounts are retired (UTA only)")
        info["mgnMode"] = {
            "ISOLATED_MARGIN": "isolated",
            "REGULAR_MARGIN": "cross",
            "PORTFOLIO_MARGIN": "portfolio",
        }.get(str(info.get("marginMode")))
        return info

    async def _position_rows(self, symbol: str | None) -> list[dict[str, Any]]:
        """``GET /v5/position/list`` (category=linear; ``symbol`` or ``settleCoin=USDT``), all pages."""
        params: dict[str, Any] = {"category": "linear", "limit": 200}
        if symbol is not None:
            params["symbol"] = self.to_native(symbol)
        else:
            params["settleCoin"] = "USDT"
        rows: list[dict[str, Any]] = []
        for _ in range(50):
            async with self._rate_limiter.request("query", group="private"):
                full_path, headers = self._signed_get_request("/v5/position/list", params)
                data = await self._http.get(full_path, headers=headers)
            result = self._check(data)
            page = result.get("list") if isinstance(result, dict) else None
            if not isinstance(page, list) or not all(isinstance(r, dict) for r in page):
                raise ExchangeError("bybit: incomplete position snapshot", exchange="bybit")
            rows.extend(page)
            cursor = result.get("nextPageCursor")
            if not cursor:
                return rows
            params["cursor"] = cursor
        raise ExchangeError("bybit: position snapshot did not terminate", exchange="bybit")

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        """Open linear positions (``size != 0``). ``margin_mode`` comes from the account-level mode (a unified
        account's per-position ``tradeMode`` is not authoritative); ``None`` when it cannot be told. Any failed
        or malformed read raises — it never means "flat"."""
        if self.market_type != "linear":
            return await super().fetch_positions(symbols)
        config = await self.fetch_account_config()
        rows = await self._position_rows(None)
        positions: list[Position] = []
        for r in rows:
            size = Decimal(str(r.get("size") or 0))
            if not size.is_finite():
                raise ExchangeError("bybit: incomplete position snapshot", exchange="bybit")
            if size == 0:
                continue
            if r.get("side") not in ("Buy", "Sell"):
                raise ExchangeError("bybit: position direction is unknown", exchange="bybit")
            mode = config.get("mgnMode")  # account-level; position ``tradeMode`` is deprecated (always 0)
            row = dict(r)
            row["mgnMode"] = mode
            positions.append(_parse_position(self.from_native(str(r.get("symbol", ""))), row, mode))
        if symbols is not None:
            wanted = set(symbols)
            positions = [p for p in positions if p.symbol in wanted]
        return positions

    async def fetch_leverage(self, symbol: str, mgn_mode: str = "isolated") -> list[dict[str, Any]]:
        """One row for the symbol: the venue ``/v5/position/list`` row plus ``mgnMode`` (``isolated``/``cross``/
        ``portfolio``/``None``), ``lever`` (str) and ``posMode`` (``one_way_mode`` when the only row has
        ``positionIdx`` 0, ``hedge_mode`` for idx 1+2). Works with or without an open position (Bybit returns
        an empty-size row). A row in the other mode is returned as-is — the caller compares.
        """
        if self.market_type != "linear":
            raise NotSupportedError("bybit: leverage settings require market_type='linear'")
        if mgn_mode not in ("isolated", "cross", "crossed"):
            raise InvalidOrderError("bybit: unknown margin mode", code="marginMode", exchange="bybit")
        config = await self.fetch_account_config()
        rows = await self._position_rows(symbol)
        idx = sorted(str(r.get("positionIdx")) for r in rows)
        if idx == ["0"]:
            pos_mode = "one_way_mode"
        elif idx == ["1", "2"]:
            pos_mode = "hedge_mode"
        else:
            raise ExchangeError("bybit: incomplete leverage snapshot", exchange="bybit")
        row = dict(rows[0])
        mode = config.get("mgnMode")  # account-level; position ``tradeMode`` is deprecated (always 0)
        row["mgnMode"] = mode
        row["posMode"] = pos_mode
        row["lever"] = None if row.get("leverage") in (None, "") else str(row["leverage"])
        return [row]

    async def set_margin_mode(
        self, symbol: str, mgn_mode: str = "isolated", *, lever: float | None = None
    ) -> dict[str, Any]:
        """Switch the **unified account's** margin mode: ``POST /v5/account/set-margin-mode``
        (``ISOLATED_MARGIN``/``REGULAR_MARGIN``). ``symbol`` and ``lever`` are accepted for call-shape
        compatibility only and are ignored — the request names no symbol.

        🚨 **Account-wide.** Bybit changes the margin mode of the whole UTA (every linear symbol, other open
        perpetual positions included; spot margin turns off under isolated). Bybit does *not* require the account
        to be flat: its documented conditions for Cross -> Isolated are no options orders/positions, no spot
        margin orders, sufficient margin and no borrowing (Bybit help: "Differences Between the Margin Modes
        Under the UTA"). Refusals come back in ``result.reasons`` and are raised here. This method does not
        check anything else — a caller that must not disturb other positions has to read the account first.
        Already in the requested mode: no request. Classic accounts no longer exist (refused).
        """
        if self.market_type != "linear":
            raise NotSupportedError("bybit: margin mode applies to market_type='linear' only")
        if mgn_mode not in ("isolated", "cross", "crossed"):
            raise InvalidOrderError(
                f"bybit: mgn_mode must be 'cross' or 'isolated', got {mgn_mode!r}", code="marginMode", exchange="bybit"
            )
        isolated = mgn_mode == "isolated"
        config = await self.fetch_account_config()
        if config.get("mgnMode") == ("isolated" if isolated else "cross"):
            return {}
        if config.get("mgnMode") is None:
            raise ExchangeError("bybit: account margin mode is unknown", code="marginMode", exchange="bybit")
        body = {"setMarginMode": "ISOLATED_MARGIN" if isolated else "REGULAR_MARGIN"}
        async with self._rate_limiter.request("order"):
            body_str = json.dumps(body)
            data = await self._http.post_raw(
                "/v5/account/set-margin-mode", body=body_str, headers=self._auth_post_headers(body_str)
            )
        result = self._check(data)
        reasons = result.get("reasons") if isinstance(result, dict) else None
        if reasons:
            raise ExchangeError(
                f"bybit: margin mode change refused: {reasons!r}", code="setMarginMode", exchange="bybit"
            )
        return result if isinstance(result, dict) else {}

    async def set_leverage(
        self, symbol: str, lever: float, mgn_mode: str = "cross", *, ensure_margin_mode: bool = False
    ) -> dict[str, Any]:
        """``POST /v5/position/set-leverage`` (buyLeverage = sellLeverage, one-way). retCode ``110043``
        ("leverage not modified" = already that value) is success. ``ensure_margin_mode=True`` first reads the
        margin mode and calls :meth:`set_margin_mode` only when it differs. No risk cap lives here.
        """
        if self.market_type != "linear":
            raise NotSupportedError("bybit: leverage applies to market_type='linear' only")
        lev = _bybit_leverage(lever)
        if mgn_mode not in ("isolated", "cross", "crossed"):
            raise InvalidOrderError("bybit: unknown margin mode", code="marginMode", exchange="bybit")
        if ensure_margin_mode:
            current = (await self.fetch_leverage(symbol, mgn_mode))[0]["mgnMode"]
            if current != ("isolated" if mgn_mode == "isolated" else "cross"):
                await self.set_margin_mode(symbol, mgn_mode, lever=lever)
        body = {"category": "linear", "symbol": self.to_native(symbol), "buyLeverage": lev, "sellLeverage": lev}
        try:
            async with self._rate_limiter.request("order"):
                body_str = json.dumps(body)
                data = await self._http.post_raw(
                    "/v5/position/set-leverage", body=body_str, headers=self._auth_post_headers(body_str)
                )
            result = self._check(data)
        except ExchangeError as e:
            if str(e.code) == "110043":
                return {}
            raise
        return result if isinstance(result, dict) else {}


# ── Parsers ──


def _parse_ticker(symbol: str, d: dict[str, Any], *, timestamp: int = 0) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("lastPrice", 0)),
        bid=float(d.get("bid1Price", 0)),
        ask=float(d.get("ask1Price", 0)),
        high=float(d.get("highPrice24h", 0)),
        low=float(d.get("lowPrice24h", 0)),
        volume=float(d.get("volume24h", 0)),
        quote_volume=float(d.get("turnover24h", 0)),
        timestamp=timestamp,
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


def _parse_order(symbol: str, d: dict[str, Any], market_type: str = "spot") -> Order:
    spot = market_type == "spot"
    quote_amount = (
        spot
        and d.get("orderType", "").lower() == "market"
        and (d.get("marketUnit") or ("quoteCoin" if d.get("side", "").lower() == "buy" else "baseCoin")) == "quoteCoin"
    )
    requested = Decimal(str(d.get("qty") or 0))
    executed = Decimal(str(d.get("cumExecQty") or 0))
    terminal = d.get("orderStatus", "").lower() in (
        "filled",
        "cancelled",
        "partiallyfilledcanceled",
        "rejected",
        "deactivated",
    )
    remaining = (
        (Decimal(0) if terminal else None if quote_amount else max(Decimal(0), requested - executed)) if spot else None
    )
    cost = Decimal(str(d["cumExecValue"])) if spot and d.get("cumExecValue") not in (None, "") else None
    return Order(
        id=d.get("orderId", ""),
        client_order_id=d.get("orderLinkId") or None,
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("orderType", "").lower(),
        amount=0.0 if quote_amount else float(requested),
        amount_exact=requested if spot and not quote_amount else None,
        amount_quote=requested if quote_amount else None,
        filled_exact=executed if spot else None,
        cost=cost,
        remaining=float(remaining) if remaining is not None else None,
        remaining_exact=remaining,
        price=float(d["price"]) if d.get("price") and float(d["price"]) > 0 else None,
        filled=float(d.get("cumExecQty", 0)),
        status=d.get("orderStatus", ""),
        timestamp=int(d.get("createdTime", 0)),
        raw=d,
    )


def _bybit_leverage(lever: float | None) -> str:
    if lever is None or isinstance(lever, bool) or not math.isfinite(lever) or lever <= 0:
        raise InvalidOrderError(
            f"bybit: lever must be a finite positive number, got {lever!r}", code="leverage", exchange="bybit"
        )
    return str(int(lever)) if float(lever).is_integer() else str(lever)


def _none_if_zero(v: Any) -> float | None:
    f = float(v or 0)
    return f if f else None


def _parse_position(symbol: str, d: dict[str, Any], margin_mode: str | None) -> Position:
    return Position(
        symbol=symbol,
        side="long" if d.get("side") == "Buy" else "short",
        amount=abs(float(d.get("size") or 0)),
        entry_price=_none_if_zero(d.get("avgPrice")),
        unrealized_pnl=float(d.get("unrealisedPnl") or 0),
        leverage=_none_if_zero(d.get("leverage")),
        liquidation_price=_none_if_zero(d.get("liqPrice")),
        margin_mode=margin_mode if margin_mode in ("cross", "isolated") else None,
        timestamp=int(d.get("updatedTime") or 0),
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
_DUPLICATE_ORDER_CODES = frozenset({"110072", "170141"})
_RATE_LIMIT_CODES = frozenset({"10006"})


def _map_error(code: str, msg: str) -> PyCexError:
    if code in _INSUFFICIENT_BALANCE_CODES:
        return InsufficientBalanceError(msg, code=code, exchange="bybit")
    if code in _AUTH_CODES:
        return AuthenticationError(msg)
    if code in _ORDER_NOT_FOUND_CODES:
        return OrderNotFoundError(msg, code=code, exchange="bybit")
    if code in _DUPLICATE_ORDER_CODES:
        return DuplicateOrderError(msg, code=code, exchange="bybit")
    if code in _RATE_LIMIT_CODES:
        return RateLimitError(msg, code=code, exchange="bybit")
    return ExchangeError(msg, code=code, exchange="bybit")


def _validated_client_order_id(value: str) -> str:
    if not _CLIENT_ORDER_ID_RE.fullmatch(value):
        raise InvalidOrderError("bybit: client_order_id must be 1-36 ASCII letters, digits, - or _", exchange="bybit")
    return value


def _error_mapper(status: int, data: dict[str, Any]) -> PyCexError | None:
    code = data.get("retCode")
    if code is None or str(code) == "0":
        return None
    return _map_error(str(code), str(data.get("retMsg", "Unknown error")))

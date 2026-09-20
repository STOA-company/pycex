"""Bitget V2 exchange adapter — spot (``/api/v2/spot/...``) and USDT-margined
linear perpetual futures (``/api/v2/mix/...``, ``market_type="linear"``).

Both market types share the same host (``api.bitget.com``); only the path
prefix and a mandatory ``productType`` param (``"USDT-FUTURES"``, or
``"SUSDT-FUTURES"`` in sandbox mode) differ for ``linear``, so routing is a
``_PATHS`` lookup table (see :meth:`Bitget._p`) rather than per-method
``if market_type`` branching, matching the Binance adapter's convention.

Doc verification (2026-08-30): Bitget's ``api-doc`` site is a client-rendered
SPA — every URL in the brief (``Get-All-Symbols-Contracts``, ``Get-Candle-
Data``, ``Get-History-Candle-Data``, ``Get-Current-Funding-Rate``) returned
via ``WebFetch`` as the same generic "Unified Trading Account" landing
content instead of the endpoint page (same failure mode noted in the OKX
module docstring). Cross-checked instead against ccxt's ``ts/src/bitget.ts``
(fetched directly via ``curl`` and grepped locally — a well-established,
independently maintained third-party mapping of Bitget's real wire format):

- Endpoint paths confirmed verbatim: ``v2/mix/market/contracts``,
  ``v2/mix/market/candles``, ``v2/mix/market/history-candles``,
  ``v2/mix/market/current-fund-rate``, ``v2/mix/position/all-position``,
  ``v2/mix/order/fills``, ``v2/mix/order/place-order``,
  ``v2/mix/order/cancel-order``, ``v2/mix/order/detail``,
  ``v2/mix/order/orders-pending``, ``v2/mix/account/accounts`` (plural — not
  the singular ``v2/mix/account/account`` a naive guess might produce).
- ``fetch_markets`` (mix): confirmed field names ``symbol, baseCoin,
  quoteCoin, pricePlace, sizeMultiplier, minTradeUSDT, symbolStatus`` (value
  ``"normal"`` for active) via ccxt's inline comment dump of a real recorded
  contracts response, matching the local fixture
  ``tests/fixtures/bitget/markets_linear.json`` field-for-field.
- Candle array shape ``[ts, open, high, low, close, baseVolume, quoteVolume]``
  confirmed (7 elements, ``ts`` = bar open ms) — matches
  ``tests/fixtures/bitget/candles_linear_1d.json``. Limits: the "recent"
  endpoint (``market/candles``) caps at 1000 (default 100); "history-candles"
  caps at 200 — ccxt takes the tighter of the two for its shared pagination
  path, so ``candle_page_limit = 200`` here too (same reasoning as OKX's
  ``candle_page_limit = 100`` for its own tighter-of-two-endpoints cap).
  Granularity value for ``1d`` is ``"1Dutc"`` (UTC-aligned bar boundaries),
  not the ``"1D"``/``"1day"`` a naive guess might produce — ccxt's swap
  timeframe table maps ``1d -> 1Dutc`` specifically, which matches this
  library's UTC-epoch-ms candle contract.
- ``fetch_funding_rate``: fixture ``tests/fixtures/bitget/funding.json`` and
  ccxt's ``parseFundingRate`` agree on ``fundingRate`` (rate) and
  ``fundingRateInterval`` (hours, present per-symbol as ``"8"`` here) plus
  ``nextUpdate`` (ms) for the next funding timestamp — *not* ``nextFundingTime``
  as an initial guess from the OKX/Binance field name might suggest. The brief
  said to hardcode ``interval_hours=8``; since the real field is present and
  the fixture's value is ``"8"`` anyway, this adapter reads it dynamically
  (``int(fundingRateInterval)``, falling back to 8) — strictly more correct,
  same fixture-observed value, so no behavior change from the brief's target.
- ``fetch_positions`` (``all-position``): bare list under ``data`` (not
  wrapped), fields ``symbol, holdSide, total, openPriceAvg, unrealizedPL,
  leverage, liquidationPrice, cTime`` confirmed via ccxt's ``parsePosition``
  comment block.
- ``fetch_my_trades`` (mix ``order/fills``): **not** a bare list — ``data`` is
  ``{"fillList": [...], "endId": "..."}`` (confirmed via ccxt's
  ``this.safeList(data, 'fillList', [])`` for swap/future, vs. a bare list for
  plain spot fills). This adapter's generic ``_check`` (which wraps a non-list
  ``data`` into a one-item list) can't unwrap this on its own, so
  ``fetch_my_trades``/``fetch_open_orders`` for ``linear`` pull the nested
  ``fillList``/``entrustedList`` key out of ``_check(data)[0]`` explicitly.
  Per-fill field names for mix differ from spot too: ``price``/``baseVolume``
  (not spot's ``priceAvg``/``size``), and ``feeDetail`` is a *list* of
  ``{feeCoin, totalFee}`` dicts for mix vs. a single dict for spot.
- ``create_order``/``cancel_order`` rejections: unlike OKX (which wraps a
  ``code=="0"`` success with a per-item ``sCode``/``sMsg`` rejection inside
  ``data[0]``), Bitget's place-order/cancel-order responses in ccxt are a bare
  ``{"orderId": ..., "clientOid": ...}`` dict with no secondary rejection
  field — the top-level ``code`` (already handled by ``_check``) is Bitget's
  only rejection signal for these two endpoints. No additional per-item
  rejection helper is needed here.
- Error codes (from ccxt's exception-code table, itself built from Bitget's
  published error-code list): auth (``40001-40012``, ``40037``), insufficient
  balance (``40711, 40712, 40762, 43012``), order not found (``40109, 40768,
  43001``), rate limit (``1001``, plus generic HTTP ``429`` already handled by
  ``HTTPClient._handle_response``).
- Sandbox/demo naming (brief calls out this exact point as needing
  verification): Bitget actually exposes **two independent** demo mechanisms,
  confirmed from ccxt's own request-builder (``sign()`` method) --
  (1) a header-only mode (``PAPTRADING: 1`` on top of the *live* host with
  ordinary symbols/productTypes — this is what ccxt's own ``sandboxMode``
  flag uses by default), and (2) a dedicated simulated-trading productType
  family (``SUSDT-FUTURES``/``SCOIN-FUTURES``/``SUSDC-FUTURES``) with
  ``S``-prefixed native symbols (e.g. ``SBTCSUSDT``) that trade against a
  separate demo orderbook. ccxt's own ``sign()`` explicitly *skips* adding the
  ``PAPTRADING`` header when the request's ``productType`` is already one of
  the ``S*`` variants, treating the two as alternatives rather than something
  to combine. The brief specifies mechanism (2) for
  ``sandbox=True, market_type="linear"`` (``productType ==
  "SUSDT-FUTURES"``, native symbol ``S{base}S{quote}``), which this adapter
  implements; per the ccxt precedent above, the ``paptrading`` header is
  *not* also sent in that case (only for spot sandbox, unchanged from the
  existing/previously-shipped behavior).

Inverse (coin-margined) perpetuals and hedge-mode (``tradeSide``) are out of
scope — only USDT-settled linear in one-way mode, matching the OKX/Binance
adapters' posture in this phase.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from pycex.auth import bitget_headers
from pycex.base import BaseExchange
from pycex.constants import BITGET_BASE, BITGET_BROKER_ID, QUOTE_SUFFIXES
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    HedgeModeNotSupportedError,
    InsufficientBalanceError,
    InvalidOrderError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
    SymbolNotFoundError,
    UnsupportedOrderError,
)
from pycex.http import HTTPClient
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.candle import Candle
from pycex.models.funding import FundingRate
from pycex.models.market import Market
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

# 🚨 Live-verified 2026-08-30: Bitget spot's plain "1day" bar aligns to Hong
# Kong time (UTC+8), same trap as OKX's bare "1D" — `granularity=1day`
# returns bars exactly 28_800_000ms (8h) earlier than `granularity=1Dutc` for
# the same symbol (diffed live: 1day gives ts=1788019200000, 1Dutc gives
# ts=1788048000000; re-confirmed against the re-recorded fixture
# tests/fixtures/bitget/candles_linear_1d.json, see tests/fixtures/NOTES.md).
# Spot supports the same "*utc" suffix family as mix, so daily bars use it
# here too, matching this library's UTC-epoch-ms bar-open contract. "1w" is
# left as the bare "1week" — it is not in BaseExchange.supported_timeframes
# (this library's timeframe contract is 1m/5m/15m/1h/4h/1d only) and its
# Hong-Kong-time behavior was not live-verified in this pass.
_TIMEFRAME_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1Dutc",
    "1w": "1week",
}

# Confirmed via ccxt's swap timeframe table (see module docstring) — "1Dutc"
# aligns daily bars to UTC boundaries, matching this library's UTC-epoch-ms
# candle contract (the spot map above uses different, unrelated granularity
# strings and is untouched by this task).
_MIX_TIMEFRAME_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1Dutc",
}

# Endpoint prefix per market_type, keyed by a logical name — a table rather
# than scattered `if self.market_type == "linear"` branches per method (same
# convention as the Binance adapter's `_PATHS`).
_PATHS: dict[str, dict[str, str]] = {
    "spot": {
        "ticker": "/api/v2/spot/market/tickers",
        "orderbook": "/api/v2/spot/market/orderbook",
        "candles": "/api/v2/spot/market/candles",
        "trades": "/api/v2/spot/market/fills",
        "markets": "/api/v2/spot/public/symbols",
        "balance": "/api/v2/spot/account/assets",
        "order": "/api/v2/spot/trade/place-order",
        "cancel": "/api/v2/spot/trade/cancel-order",
        "orderInfo": "/api/v2/spot/trade/orderInfo",
        "openOrders": "/api/v2/spot/trade/unfilled-orders",
        "fills": "/api/v2/spot/trade/fills",
    },
    "linear": {
        "ticker": "/api/v2/mix/market/ticker",
        "orderbook": "/api/v2/mix/market/merge-depth",
        "candles": "/api/v2/mix/market/candles",
        "historyCandles": "/api/v2/mix/market/history-candles",
        "trades": "/api/v2/mix/market/fills",
        "markets": "/api/v2/mix/market/contracts",
        "funding": "/api/v2/mix/market/current-fund-rate",
        "positions": "/api/v2/mix/position/all-position",
        "balance": "/api/v2/mix/account/accounts",
        "order": "/api/v2/mix/order/place-order",
        "cancel": "/api/v2/mix/order/cancel-order",
        "orderInfo": "/api/v2/mix/order/detail",
        "openOrders": "/api/v2/mix/order/orders-pending",
        "fills": "/api/v2/mix/order/fills",
    },
}


class Bitget(BaseExchange):
    """Bitget V2 spot exchange and USDT-margined linear perpetual futures.

    Paper trading: pass ``sandbox=True``. For ``market_type="spot"`` this
    routes private calls to Bitget's header-based demo trading
    (``paptrading: 1``, requires a demo API key). For ``market_type=
    "linear"`` this instead routes to Bitget's dedicated simulated-futures
    productType (``SUSDT-FUTURES``) with ``S``-prefixed native symbols — see
    the module docstring for why these are two different (non-combined)
    mechanisms.
    """

    name = "bitget"
    candle_page_limit = 200
    # `startTime` is only a floor on both market types: the page served is the NEWEST
    # `limit` bars at or before `endTime` (live probe 2026-08-30: spot 1d, since =
    # now-400d -> 2026-02-12..2026-08-30). On mix, sending both bounds over a wide
    # window is a hard error ("startTime and endTime interval cannot be greater than
    # 90 days"), which a backward walk never triggers because it only ever sends
    # `endTime`.
    candle_paging = "backward"

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        demo: bool | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._secret = secret
        self._passphrase = passphrase
        self.market_type = market_type
        self.sandbox = self._resolve_sandbox(sandbox, None, demo)
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        self._product_type = "SUSDT-FUTURES" if (market_type == "linear" and self.sandbox) else "USDT-FUTURES"
        broker_headers: dict[str, str] = {}
        if BITGET_BROKER_ID:
            broker_headers["X-CHANNEL-API-CODE"] = BITGET_BROKER_ID
        self._http = HTTPClient(
            # ExchangeRateLimiter is the sole admission gate; keep HTTPClient from delaying after signing.
            BITGET_BASE,
            timeout=timeout,
            rate=float("inf"),
            default_headers=broker_headers,
            error_mapper=_error_mapper,
        )

    def _p(self, name: str) -> str:
        return _PATHS[self.market_type][name]

    def to_native(self, symbol: str) -> str:
        sym = parse_symbol(symbol)
        if self.market_type == "linear":
            if sym.settle is not None and sym.settle != sym.quote:
                raise SymbolNotFoundError(
                    f"bitget: inverse/cross-settle perpetuals are out of scope (USDT-settled only): {symbol!r}"
                )
            prefix = "S" if self.sandbox else ""
            return f"{prefix}{sym.base}{prefix}{sym.quote}"
        return f"{sym.base}{sym.quote}"

    def from_native(self, native: str) -> str:
        if native in self._markets:
            return self._markets[native].symbol
        if self.market_type == "linear":
            suffixes = [f"S{q}" for q in QUOTE_SUFFIXES] if self.sandbox else list(QUOTE_SUFFIXES)
            for suf in suffixes:
                if native.endswith(suf) and len(native) > len(suf):
                    base = native[: -len(suf)]
                    quote = suf[1:] if self.sandbox else suf
                    if self.sandbox and base.startswith("S"):
                        base = base[1:]
                    return make_linear_symbol(base, quote, quote)
            raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")
        for quote in QUOTE_SUFFIXES:
            if native.endswith(quote) and len(native) > len(quote):
                return make_spot_symbol(native[: -len(quote)], quote)
        raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")

    def _check(self, data: dict[str, Any]) -> list[Any]:
        # Bitget wraps success as code "00000"; auth/API errors arrive as HTTP 200 + non-zero code.
        code = data.get("code", "00000")
        if code != "00000":
            raise _map_error(str(code), data.get("msg", "Unknown error"))
        result = data.get("data", [])
        return result if isinstance(result, list) else [result]

    def _unwrap(self, data: dict[str, Any], key: str) -> list[Any]:
        """Unwrap a mix endpoint whose ``data`` is ``{key: [...], ...cursor fields}``
        rather than a bare list (see module docstring: ``order/fills`` ->
        ``fillList``, ``order/orders-pending`` -> ``entrustedList``)."""
        result = self._check(data)
        if result and isinstance(result[0], dict):
            items = result[0].get(key, [])
            return items if isinstance(items, list) else []
        return []

    def _signed_get(self, path: str) -> dict[str, str]:
        demo = self.sandbox and self.market_type != "linear"
        return bitget_headers(self._api_key, self._secret, self._passphrase, "GET", path, "", demo=demo)

    def _signed_post(self, path: str, body: str) -> dict[str, str]:
        demo = self.sandbox and self.market_type != "linear"
        return bitget_headers(self._api_key, self._secret, self._passphrase, "POST", path, body, demo=demo)

    @staticmethod
    def _path(path: str, params: dict[str, Any] | None) -> str:
        # Sign over the exact query string we send → build it into the path and pass params=None.
        return f"{path}?{urlencode(params)}" if params else path

    def _mix_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"productType": self._product_type}
        if extra:
            params.update(extra)
        return params

    # ── Market Data (public, no signing) ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        params: dict[str, Any] = {"symbol": native}
        if self.market_type == "linear":
            params = self._mix_params(params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("ticker"), params=params)
        return _parse_ticker(symbol, self._check(data)[0])

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        native = self.to_native(symbol)
        params: dict[str, Any] = {"symbol": native, "limit": limit}
        if self.market_type == "linear":
            params = self._mix_params(params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("orderbook"), params=params)
        return _parse_order_book(symbol, self._check(data)[0])

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        if self.market_type == "linear":
            limit = min(limit, 200)
            params: dict[str, Any] = self._mix_params(
                {"symbol": native, "granularity": _MIX_TIMEFRAME_MAP.get(timeframe, timeframe), "limit": limit}
            )
            if since is not None:
                params["startTime"] = since
            if until is not None:
                params["endTime"] = until
            async with self._rate_limiter.request("query"):
                data = await self._http.get(self._p("candles"), params=params)
            result = self._check(data)
            if not result:
                # Mirrors OKX's recent-endpoint-empty -> history-endpoint fallback
                # (see module docstring): not independently confirmed live for
                # Bitget in this session, but harmless when the recent endpoint
                # already returns data (this branch is then never taken).
                async with self._rate_limiter.request("query"):
                    data = await self._http.get(self._p("historyCandles"), params=params)
                result = self._check(data)
            candles = [_parse_candle(k) for k in result]
            candles.sort(key=lambda c: c.timestamp)
            return candles
        params = {
            "symbol": native,
            "granularity": _TIMEFRAME_MAP.get(timeframe, timeframe),
            "limit": limit,
        }
        if since is not None:
            params["startTime"] = since
        if until is not None:
            params["endTime"] = until
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("candles"), params=params)
        # Sorted for the same reason the mix branch above is: ascending is the
        # library-wide contract, and it must not depend on which market type
        # (or which of Bitget's two candle endpoints) served the page.
        return sorted((_parse_candle(k) for k in self._check(data)), key=lambda c: c.timestamp)

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        params: dict[str, Any] = {"symbol": native, "limit": limit}
        if self.market_type == "linear":
            params = self._mix_params(params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("trades"), params=params)
        return [_parse_trade(symbol, t) for t in self._check(data)]

    async def fetch_markets(self) -> list[Market]:
        params = self._mix_params() if self.market_type == "linear" else None
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("markets"), params=params)
        markets = [_parse_market(d, self.market_type) for d in self._check(data)]
        self._markets = {m.native: m for m in markets}
        return markets

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        params = self._mix_params() if self.market_type == "linear" else None
        path = self._path(self._p("balance"), params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._signed_get(path))
        result = self._check(data)
        if self.market_type == "linear":
            return _parse_balance_linear(result, data)
        return _parse_balance(result, data)

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        if self.market_type != "linear":
            return await super().fetch_positions(symbols)
        params = self._mix_params({"marginCoin": "USDT"})
        path = self._path(self._p("positions"), params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._signed_get(path))
        result = self._check(data)
        positions = [
            _parse_position(self.from_native(str(p.get("symbol", ""))), p)
            for p in result
            if float(p.get("total", 0) or 0) != 0
        ]
        if symbols is not None:
            wanted = set(symbols)
            positions = [p for p in positions if p.symbol in wanted]
        return positions

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        if self.market_type != "linear":
            return await super().fetch_funding_rate(symbol)
        native = self.to_native(symbol)
        params = self._mix_params({"symbol": native})
        async with self._rate_limiter.request("query"):
            data = await self._http.get(self._p("funding"), params=params)
        result = self._check(data)
        return _parse_funding(symbol, result[0] if result else {})

    # ── Trading ──

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        if reduce_only and self.market_type != "linear":
            raise UnsupportedOrderError("reduce_only requires a linear futures market")
        native = self.to_native(symbol)
        path = self._p("order")
        body: dict[str, Any]
        if self.market_type == "linear":
            account_path = self._path(
                "/api/v2/mix/account/account", self._mix_params({"symbol": native, "marginCoin": "USDT"})
            )
            async with self._rate_limiter.request("query"):
                account = self._check(await self._http.get(account_path, headers=self._signed_get(account_path)))
            mode = account[0].get("posMode") if account and isinstance(account[0], dict) else None
            if mode == "hedge_mode":
                raise HedgeModeNotSupportedError("Hedge-mode accounts are not supported", exchange=self.name)
            if mode != "one_way_mode":
                raise ExchangeError("Cannot determine account position mode", exchange=self.name)
            body = {
                "symbol": native,
                "productType": self._product_type,
                "marginMode": "crossed",
                "marginCoin": "USDT",
                "size": str(amount),
                "side": side.lower(),
                "orderType": "limit" if order_type.lower() == "limit" else "market",
            }
            if order_type.lower() == "limit":
                body["force"] = "gtc"
                if price is not None:
                    body["price"] = str(price)
        else:
            body = {
                "symbol": native,
                "side": side.lower(),
                "orderType": "limit" if order_type.lower() == "limit" else "market",
                "size": str(amount),
            }
            if order_type.lower() == "limit":
                body["force"] = "gtc"
                if price is not None:
                    body["price"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "YES"
        if client_order_id is not None:
            body["clientOid"] = client_order_id
        body_str = json.dumps(body)
        async with self._rate_limiter.request("order"):
            data = await self._http.post_raw(path, body=body_str, headers=self._signed_post(path, body_str))
        r = self._check(data)
        first = r[0] if r else {}
        return Order(
            id=first.get("orderId", ""),
            client_order_id=first.get("clientOid") or client_order_id or None,
            status="accepted"
            if self.market_type == "linear" and (first.get("orderId") or first.get("clientOid"))
            else "",
            symbol=symbol,
            side=side.lower(),
            type=order_type.lower(),
            amount=amount,
            price=price,
            raw=data,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        path = self._p("cancel")
        body: dict[str, Any]
        if self.market_type == "linear":
            body = self._mix_params({"symbol": native, "orderId": order_id})
        else:
            body = {"symbol": native, "orderId": order_id}
        body_str = json.dumps(body)
        async with self._rate_limiter.request("order"):
            data = await self._http.post_raw(path, body=body_str, headers=self._signed_post(path, body_str))
        r = self._check(data)
        first = r[0] if r else {}
        return Order(id=first.get("orderId", order_id), symbol=symbol, side="", type="", amount=0, raw=data)

    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order:
        if bool(order_id) == bool(client_order_id):
            raise InvalidOrderError("Provide exactly one of order_id or client_order_id")
        native = self.to_native(symbol)
        lookup = {"clientOid": client_order_id} if client_order_id else {"orderId": order_id}
        params: dict[str, Any]
        if self.market_type == "linear":
            params = self._mix_params({"symbol": native, **lookup})
        else:
            # Bitget's spot order-info endpoint is keyed by orderId only — no symbol filter to convert.
            params = lookup
        path = self._path(self._p("orderInfo"), params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._signed_get(path))
        r = self._check(data)
        if not r:
            if self.market_type == "linear":
                raise OrderNotFoundError("Order lookup returned no rows", exchange=self.name)
            return Order(id=order_id or "", symbol=symbol, side="", type="", amount=0)
        return _parse_order_mix(symbol, r[0]) if self.market_type == "linear" else _parse_order(symbol, r[0])

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        native = self.to_native(symbol) if symbol else None
        if self.market_type == "linear":
            params: dict[str, Any] = self._mix_params({"symbol": native} if native else None)
            path = self._path(self._p("openOrders"), params)
            async with self._rate_limiter.request("query"):
                data = await self._http.get(path, headers=self._signed_get(path))
            items = self._unwrap(data, "entrustedList")
            return [_parse_order_mix(self.from_native(o.get("symbol", "")), o) for o in items]
        path = self._path(self._p("openOrders"), {"symbol": native} if native else None)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._signed_get(path))
        return [_parse_order(self.from_native(o.get("symbol", "")), o) for o in self._check(data)]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        if symbol is None:
            raise ValueError(f"bitget:{self.market_type} requires symbol for my trades")
        native = self.to_native(symbol)
        if self.market_type == "linear":
            params: dict[str, Any] = self._mix_params({"symbol": native})
            if since is not None:
                params["startTime"] = str(since)
            if limit is not None:
                params["limit"] = str(limit)
            path = self._path(self._p("fills"), params)
            async with self._rate_limiter.request("query"):
                data = await self._http.get(path, headers=self._signed_get(path))
            fills = self._unwrap(data, "fillList")
            return [_parse_my_trade_mix(symbol, t) for t in fills]
        params = {"symbol": native}
        if since is not None:
            params["startTime"] = str(since)
        if limit is not None:
            params["limit"] = str(limit)
        path = self._path(self._p("fills"), params)
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._signed_get(path))
        result = self._check(data)
        return [_parse_my_trade_spot(symbol, t) for t in result]


# ── Parsers ──


def _parse_ticker(symbol: str, d: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("lastPr", 0) or 0),
        bid=float(d.get("bidPr", 0) or 0),
        ask=float(d.get("askPr", 0) or 0),
        high=float(d.get("high24h", 0) or 0),
        low=float(d.get("low24h", 0) or 0),
        volume=float(d.get("baseVolume", 0) or 0),
        quote_volume=float(d.get("quoteVolume", 0) or 0),
        timestamp=int(d.get("ts", 0) or 0),
        raw=d,
    )


def _parse_order_book(symbol: str, d: dict[str, Any]) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookEntry(price=float(b[0]), amount=float(b[1])) for b in d.get("bids", [])],
        asks=[OrderBookEntry(price=float(a[0]), amount=float(a[1])) for a in d.get("asks", [])],
        timestamp=int(d.get("ts", 0) or 0),
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
        id=str(t.get("tradeId", "")),
        symbol=symbol,
        side=t.get("side", "").lower(),
        price=float(t.get("price", 0) or 0),
        amount=float(t.get("size", 0) or 0),
        timestamp=int(t.get("ts", 0) or 0),
    )


def _parse_market(d: dict[str, Any], market_type: MarketType) -> Market:
    """Parse one ``fetch_markets`` row.

    spot: ``pricePrecision``/``quantityPrecision`` are decimal-place *counts*
    (``price_tick = 10 ** -pricePrecision``). mix: ``pricePlace`` is likewise a
    decimal-place count (``price_tick = 10 ** -pricePlace``), but
    ``sizeMultiplier`` is already the step itself (used directly as
    ``amount_step``, not exponentiated) — the two market types encode amount
    precision differently, confirmed against the recorded
    ``markets_spot``/``markets_linear`` fixtures.
    """
    native = str(d.get("symbol", ""))
    base = str(d.get("baseCoin", ""))
    quote = str(d.get("quoteCoin", ""))
    min_notional_raw = d.get("minTradeUSDT")
    min_notional = float(min_notional_raw) if min_notional_raw not in (None, "") else None
    if market_type == "linear":
        symbol = make_linear_symbol(base, quote, quote)
        price_place = d.get("pricePlace")
        price_tick = 10 ** -int(price_place) if price_place not in (None, "") else None
        size_multiplier = d.get("sizeMultiplier")
        amount_step = float(size_multiplier) if size_multiplier not in (None, "") else None
        active = d.get("symbolStatus") == "normal"
    else:
        symbol = make_spot_symbol(base, quote)
        price_precision = d.get("pricePrecision")
        price_tick = 10 ** -int(price_precision) if price_precision not in (None, "") else None
        qty_precision = d.get("quantityPrecision")
        amount_step = 10 ** -int(qty_precision) if qty_precision not in (None, "") else None
        active = d.get("status") == "online"
    return Market(
        symbol=symbol,
        native=native,
        base=base,
        quote=quote,
        market_type=market_type,
        price_tick=price_tick,
        amount_step=amount_step,
        min_notional=min_notional,
        amount_unit="base" if market_type == "linear" else None,
        contract_size=1.0 if market_type == "linear" else None,
        min_amount=float(d["minTradeNum"]) if market_type == "linear" and d.get("minTradeNum") else None,
        active=active,
        raw=d,
    )


def _parse_balance(result: list[Any], raw: dict[str, Any]) -> Balance:
    entries = []
    for coin in result:
        free = float(coin.get("available", 0) or 0)
        locked = float(coin.get("frozen", 0) or 0) + float(coin.get("locked", 0) or 0)
        if free > 0 or locked > 0:
            entries.append(BalanceEntry(asset=coin.get("coin", ""), free=free, locked=locked))
    return Balance(assets=entries, raw=raw)


def _parse_balance_linear(result: list[Any], raw: dict[str, Any]) -> Balance:
    """Parse ``GET /api/v2/mix/account/accounts`` — bare list under ``data``,
    fields ``marginCoin, available, locked`` (confirmed via ccxt)."""
    entries = []
    for acc in result:
        free = float(acc.get("available", 0) or 0)
        locked = float(acc.get("locked", 0) or 0)
        if free > 0 or locked > 0:
            entries.append(BalanceEntry(asset=str(acc.get("marginCoin", "")), free=free, locked=locked))
    return Balance(assets=entries, raw=raw)


def _none_if_zero(v: Any) -> float | None:
    """``0``/``""``/missing all mean "not meaningful" (e.g. ``liquidationPrice``
    is ``"0"`` when Bitget can't compute one for a low-risk position) — same
    treatment as the OKX/Binance adapters' equivalent helper."""
    if v in (None, ""):
        return None
    f = float(v)
    return f if f != 0 else None


def _parse_position(symbol: str, d: dict[str, Any]) -> Position:
    total = float(d.get("total", 0) or 0)
    hold_side = str(d.get("holdSide", "")).lower()
    side = hold_side if hold_side in ("long", "short") else ("long" if total >= 0 else "short")
    return Position(
        symbol=symbol,
        side=side,
        amount=abs(total),
        entry_price=_none_if_zero(d.get("openPriceAvg")),
        unrealized_pnl=float(d.get("unrealizedPL", 0) or 0),
        leverage=_none_if_zero(d.get("leverage")),
        liquidation_price=_none_if_zero(d.get("liquidationPrice")),
        timestamp=int(d.get("cTime", 0) or 0),
        raw=d,
    )


def _parse_funding(symbol: str, d: dict[str, Any]) -> FundingRate:
    interval = d.get("fundingRateInterval")
    return FundingRate(
        symbol=symbol,
        rate=float(d.get("fundingRate", 0) or 0),
        interval_hours=int(interval) if interval not in (None, "") else 8,
        next_funding_time=int(d.get("nextUpdate", 0) or 0),
        timestamp=0,
        raw=d,
    )


def _parse_my_trade_spot(symbol: str, t: dict[str, Any]) -> MyTrade:
    fee_detail = t.get("feeDetail") or {}
    return MyTrade(
        id=str(t.get("tradeId", "")),
        order_id=str(t.get("orderId", "")),
        symbol=symbol,
        side=str(t.get("side", "")).lower(),
        price=float(t.get("priceAvg", 0) or 0),
        amount=float(t.get("size", 0) or 0),
        fee=float(fee_detail.get("totalFee", 0) or 0),
        fee_asset=str(fee_detail.get("feeCoin", "") or ""),
        timestamp=int(t.get("cTime", 0) or 0),
        raw=t,
    )


def _parse_my_trade_mix(symbol: str, t: dict[str, Any]) -> MyTrade:
    """mix fills: ``price``/``baseVolume`` (not spot's ``priceAvg``/``size``);
    ``feeDetail`` is a *list* of ``{feeCoin, totalFee}`` here, not a dict."""
    fee_detail = t.get("feeDetail") or []
    fee0 = fee_detail[0] if isinstance(fee_detail, list) and fee_detail else {}
    return MyTrade(
        id=str(t.get("tradeId", "")),
        order_id=str(t.get("orderId", "")),
        symbol=symbol,
        side=str(t.get("side", "")).lower(),
        price=float(t.get("price", 0) or 0),
        amount=float(t.get("baseVolume", 0) or 0),
        fee=float(fee0.get("totalFee", 0) or 0),
        fee_asset=str(fee0.get("feeCoin", "") or ""),
        timestamp=int(t.get("cTime", 0) or 0),
        raw=t,
    )


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    price = float(d.get("price", 0) or 0)
    return Order(
        id=str(d.get("orderId", "")),
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("orderType", "").lower(),
        amount=float(d.get("size", 0) or 0),
        price=price if price > 0 else None,
        filled=float(d.get("baseVolume", 0) or 0),
        status=d.get("status", ""),
        timestamp=int(d.get("cTime", 0) or 0),
        raw=d,
    )


def _parse_order_mix(symbol: str, d: dict[str, Any]) -> Order:
    """mix order rows are inconsistent about the status field name across
    endpoints: ``/mix/order/detail`` uses ``state``, but
    ``/mix/order/orders-pending`` (``entrustedList``) uses ``status`` instead
    (ccxt handles this the same way — ``safeStringN(order, ['status',
    'state'])``). Read ``status`` first, fall back to ``state``."""
    price = float(d.get("price", 0) or 0)
    status = d.get("status")
    if status is None:
        status = d.get("state", "")
    return Order(
        id=str(d.get("orderId", "")),
        client_order_id=d.get("clientOid") or None,
        average=(float(d["priceAvg"]) or None) if d.get("priceAvg") else None,
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("orderType", "").lower(),
        amount=float(d.get("size", 0) or 0),
        price=price if price > 0 else None,
        filled=float(d.get("baseVolume", 0) or 0),
        status=status,
        timestamp=int(d.get("cTime", 0) or 0),
        raw=d,
    )


# ── Errors ──

# Confirmed via ccxt's Bitget exception-code table (see module docstring).
_AUTH_CODES = frozenset(
    {"40001", "40002", "40003", "40004", "40005", "40006", "40008", "40009", "40010", "40011", "40012", "40037"}
)
_INSUFFICIENT_BALANCE_CODES = frozenset({"40711", "40712", "40762", "43012"})
_ORDER_NOT_FOUND_CODES = frozenset({"40109", "40768", "43001"})
_RATE_LIMIT_CODES = frozenset({"1001"})


def _map_error(code: str, msg: str) -> PyCexError:
    if code == "45109":
        return HedgeModeNotSupportedError("Hedge-mode accounts are not supported", code=code, exchange="bitget")
    if code in _INSUFFICIENT_BALANCE_CODES:
        return InsufficientBalanceError(msg, code=code, exchange="bitget")
    if code in _AUTH_CODES:
        return AuthenticationError(msg)
    if code in _ORDER_NOT_FOUND_CODES:
        return OrderNotFoundError(msg, code=code, exchange="bitget")
    if code in _RATE_LIMIT_CODES:
        return RateLimitError(msg, code=code, exchange="bitget")
    return ExchangeError(msg, code=code, exchange="bitget")


def _error_mapper(status: int, data: dict[str, Any]) -> PyCexError | None:
    code = data.get("code")
    if code is None or code == "00000":
        return None
    return _map_error(str(code), str(data.get("msg", "Unknown error")))

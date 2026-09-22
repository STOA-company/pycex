"""Binance exchange adapter — spot (``/api/v3``) and USDT-M linear perpetuals (``/fapi``).

``market_type="linear"`` switches the base URL to ``fapi.binance.com`` (or its
testnet) and every endpoint to its ``/fapi/v1``|``/fapi/v2`` counterpart via the
``_PATHS`` table (see :meth:`Binance._p`), so no per-method ``if market_type``
branching is needed.

Doc verification (2026-08-30) — not a blanket "confirmed", itemized:
- Live-fetched and confirmed: ``POST /fapi/v1/order`` params
  (``symbol/side/type/quantity/price/positionSide/timeInForce``) and
  ``GET /fapi/v1/userTrades`` response fields (``id, orderId, price, qty,
  commission, commissionAsset, time, side`` — futures trades carry an explicit
  ``side``, unlike spot) from
  developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api;
  ``GET /fapi/v1/klines`` (12-column array, index 0 = open time ms),
  ``GET /fapi/v1/premiumIndex`` (``symbol, markPrice, indexPrice,
  lastFundingRate, nextFundingTime, interestRate, time``), and
  ``GET /fapi/v1/exchangeInfo`` (``symbols[].{symbol,baseAsset,quoteAsset,status}``,
  ``PRICE_FILTER.tickSize``, ``LOT_SIZE.stepSize``, ``MIN_NOTIONAL.notional``) from
  developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api;
  ``GET /api/v3/exchangeInfo`` ``symbols[].{symbol,baseAsset,quoteAsset,status}`` and
  ``PRICE_FILTER.tickSize`` from
  developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints
  (``LOT_SIZE.stepSize``/``NOTIONAL.minNotional`` for spot weren't spelled out in
  that fetch's excerpt, but are load-bearing verbatim in the recorded fixture
  ``tests/fixtures/binance/markets_spot.json``).
- Reviewer-verified 2026-08-30 (not independently live-fetched in this pass):
  ``GET /fapi/v2/positionRisk`` fields (``symbol, positionAmt, entryPrice,
  unRealizedProfit, leverage, liquidationPrice, updateTime``) and
  ``GET /fapi/v2/balance`` fields (``asset, balance, availableBalance`` — a bare
  list, not ``{"balances":[...]}`` like spot's ``/api/v3/account``).
- Not documented in either fetched page and not independently reconfirmed:
  the ``-4061`` hedge-mode error code below is long-standing, widely-documented
  Binance Futures API behavior, not a value pulled from a docs page in this
  session — treat it as unverified-by-fetch if it ever needs to be relied on
  precisely (e.g. matching on the numeric code programmatically).

Error mapping (2026-08-30, live-fetched and confirmed against
developers.binance.com/docs/binance-spot-api-docs/errors): ``-2010``
("Account has insufficient balance for requested action.") -> insufficient
balance; ``-2014``/``-2015`` (bad API-key format / invalid key-IP-permissions)
-> authentication; ``-2013`` ("Order does not exist.") -> order not found;
``-1003`` (too many requests / weight-limit / IP ban) -> rate limit. Wired via
``_error_mapper`` into ``HTTPClient`` the same way as OKX/Bitget/Bybit, so a
previously-unmapped Binance HTTP>=400 body (``{"code": ..., "msg": ...}``) now
raises the matching typed exception instead of the generic ``ExchangeError``
fallback.

``create_order`` never sends ``positionSide`` — this assumes the linear account
is in one-way mode (Binance's default). A hedge-mode account requires
``positionSide=LONG``/``SHORT`` on every order; without it Binance rejects the
order with ``ExchangeError`` code ``-4061`` ("Order's position side does not
match user's setting."), which surfaces to the caller unchanged.
"""

from __future__ import annotations

from typing import Any

from pycex.auth import binance_headers, binance_sign
from pycex.base import BaseExchange
from pycex.constants import (
    BINANCE_BASE,
    BINANCE_BROKER_ID,
    BINANCE_FAPI,
    BINANCE_FAPI_TESTNET,
    BINANCE_TESTNET,
    QUOTE_SUFFIXES,
)
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
from pycex.models.funding import FundingRate
from pycex.models.market import Market, parse_listing_time
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
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
}

# Endpoint prefix per market_type, keyed by a logical name — kept as a table
# rather than scattered `if self.market_type == "linear"` branches per method.
_PATHS: dict[str, dict[str, str]] = {
    "spot": {
        "klines": "/api/v3/klines",
        "ticker": "/api/v3/ticker/24hr",
        "depth": "/api/v3/depth",
        "trades": "/api/v3/trades",
        "exchangeInfo": "/api/v3/exchangeInfo",
        "order": "/api/v3/order",
        "openOrders": "/api/v3/openOrders",
        "userTrades": "/api/v3/myTrades",
        "balance": "/api/v3/account",
    },
    "linear": {
        "klines": "/fapi/v1/klines",
        "ticker": "/fapi/v1/ticker/24hr",
        "depth": "/fapi/v1/depth",
        "trades": "/fapi/v1/trades",
        "exchangeInfo": "/fapi/v1/exchangeInfo",
        "order": "/fapi/v1/order",
        "openOrders": "/fapi/v1/openOrders",
        "userTrades": "/fapi/v1/userTrades",
        "balance": "/fapi/v2/balance",
        "positionRisk": "/fapi/v2/positionRisk",
        "premiumIndex": "/fapi/v1/premiumIndex",
    },
}


class Binance(BaseExchange):
    name = "binance"
    # `startTime` serves the oldest `limit` bars from it (live probe 2026-08-30).
    candle_paging = "forward"

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        testnet: bool | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._secret = secret
        self.market_type = market_type
        self.sandbox = self._resolve_sandbox(sandbox, testnet, None)
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        if market_type == "linear":
            base = BINANCE_FAPI_TESTNET if self.sandbox else BINANCE_FAPI
        else:
            base = BINANCE_TESTNET if self.sandbox else BINANCE_BASE
        broker_headers: dict[str, str] = {}
        if BINANCE_BROKER_ID:
            broker_headers["X-MBX-BROKER-ID"] = BINANCE_BROKER_ID
        # Endpoint weights follow Binance REST docs:
        # https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md
        # ExchangeRateLimiter is the sole request gate; keep the legacy HTTP gate inert.
        self._http = HTTPClient(
            base, timeout=timeout, rate=float("inf"), default_headers=broker_headers, error_mapper=_error_mapper
        )

    def _p(self, name: str) -> str:
        return _PATHS[self.market_type][name]

    def to_native(self, symbol: str) -> str:
        sym = parse_symbol(symbol)
        return f"{sym.base}{sym.quote}"

    def from_native(self, native: str) -> str:
        if native in self._markets:
            return self._markets[native].symbol
        for quote in QUOTE_SUFFIXES:
            if native.endswith(quote) and len(native) > len(quote):
                base = native[: -len(quote)]
                if self.market_type == "linear":
                    return make_linear_symbol(base, quote)
                return make_spot_symbol(base, quote)
        raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")

    def _auth_headers(self) -> dict[str, str]:
        return binance_headers(self._api_key)

    def _signed_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return binance_sign(self._secret, params or {})

    # ── Market Data ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        # https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md
        async with self._rate_limiter.request("query", weight=2):
            data = await self._http.get(self._p("ticker"), params={"symbol": native})
        return _parse_ticker(symbol, data)

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        native = self.to_native(symbol)
        if self.market_type == "linear":
            weight = 2 if limit <= 100 else 5 if limit <= 500 else 10 if limit <= 1000 else 20
        else:
            weight = 5 if limit <= 100 else 25 if limit <= 500 else 50 if limit <= 1000 else 250
        async with self._rate_limiter.request("query", weight=weight):
            data = await self._http.get(self._p("depth"), params={"symbol": native, "limit": limit})
        return _parse_order_book(symbol, data)

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "symbol": native,
            "interval": _TIMEFRAME_MAP.get(timeframe, timeframe),
            "limit": limit,
        }
        if since is not None:
            params["startTime"] = since
        if until is not None:
            params["endTime"] = until
        if self.market_type == "linear":
            weight = 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
        else:
            weight = 2
        async with self._rate_limiter.request("query", weight=weight):
            data = await self._http.get(self._p("klines"), params=params)
        return [_parse_candle(k) for k in data]

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", weight=5 if self.market_type == "linear" else 25):
            data = await self._http.get(self._p("trades"), params={"symbol": native, "limit": limit})
        return [_parse_trade(symbol, t) for t in data]

    async def fetch_markets(self) -> list[Market]:
        async with self._rate_limiter.request("query", weight=1 if self.market_type == "linear" else 20):
            data = await self._http.get(self._p("exchangeInfo"))
        markets = [_parse_market(s, self.market_type) for s in data.get("symbols", [])]
        self._markets = {m.native: m for m in markets}
        return markets

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        async with self._rate_limiter.request("query", weight=5 if self.market_type == "linear" else 20):
            params = self._signed_params()
            data = await self._http.get(self._p("balance"), params=params, headers=self._auth_headers())
        if self.market_type == "linear":
            return _parse_balance_linear(data)
        return _parse_balance(data)

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        if self.market_type != "linear":
            return await super().fetch_positions(symbols)
        async with self._rate_limiter.request("query", weight=10):
            params = self._signed_params()
            data = await self._http.get(self._p("positionRisk"), params=params, headers=self._auth_headers())
        positions = [
            _parse_position(self.from_native(str(p.get("symbol", ""))), p)
            for p in data
            if float(p.get("positionAmt", 0) or 0) != 0
        ]
        if symbols is not None:
            wanted = set(symbols)
            positions = [p for p in positions if p.symbol in wanted]
        return positions

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        if self.market_type != "linear":
            return await super().fetch_funding_rate(symbol)
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", weight=1):
            data = await self._http.get(self._p("premiumIndex"), params={"symbol": native})
        return _parse_funding(symbol, data)

    # ── Trading ──

    async def create_order(
        self, symbol: str, side: str, order_type: str, amount: float, price: float | None = None
    ) -> Order:
        native = self.to_native(symbol)
        params: dict[str, Any] = {
            "symbol": native,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": str(amount),
        }
        if price is not None:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        async with self._rate_limiter.request("order", weight=1):
            params = self._signed_params(params)
            data = await self._http.post(self._p("order"), params=params, headers=self._auth_headers())
        return _parse_order(symbol, data)

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("order", weight=1):
            params = self._signed_params({"symbol": native, "orderId": order_id})
            data = await self._http.delete(self._p("order"), params=params, headers=self._auth_headers())
        return _parse_order(symbol, data)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", weight=1 if self.market_type == "linear" else 4):
            params = self._signed_params({"symbol": native, "orderId": order_id})
            data = await self._http.get(self._p("order"), params=params, headers=self._auth_headers())
        return _parse_order(symbol, data)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        p: dict[str, Any] = {}
        if symbol:
            p["symbol"] = self.to_native(symbol)
        # https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Current-All-Open-Orders
        weight = (1 if symbol else 40) if self.market_type == "linear" else (6 if symbol else 80)
        async with self._rate_limiter.request("query", weight=weight):
            params = self._signed_params(p)
            data = await self._http.get(self._p("openOrders"), params=params, headers=self._auth_headers())
        return [_parse_order(self.from_native(o.get("symbol", "")), o) for o in data]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        if symbol is None:
            raise ValueError("binance requires symbol for my trades")
        native = self.to_native(symbol)
        p: dict[str, Any] = {"symbol": native}
        if since is not None:
            p["startTime"] = since
        if limit is not None:
            p["limit"] = limit
        async with self._rate_limiter.request("query", weight=5 if self.market_type == "linear" else 20):
            params = self._signed_params(p)
            data = await self._http.get(self._p("userTrades"), params=params, headers=self._auth_headers())
        return [_parse_my_trade(symbol, t) for t in data]


# ── Parsers ──


def _parse_ticker(symbol: str, d: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("lastPrice", 0) or 0),
        bid=float(d.get("bidPrice", 0) or 0),
        ask=float(d.get("askPrice", 0) or 0),
        high=float(d.get("highPrice", 0) or 0),
        low=float(d.get("lowPrice", 0) or 0),
        volume=float(d.get("volume", 0) or 0),
        quote_volume=float(d.get("quoteVolume", 0) or 0),
        timestamp=int(d.get("closeTime", 0) or 0),
        raw=d,
    )


def _parse_order_book(symbol: str, d: dict[str, Any]) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookEntry(price=float(b[0]), amount=float(b[1])) for b in d.get("bids", [])],
        asks=[OrderBookEntry(price=float(a[0]), amount=float(a[1])) for a in d.get("asks", [])],
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
    # `isBuyerMaker=true` means the BUYER sat on the book as the maker, so the
    # aggressor SOLD. `Trade.side` is the taker side everywhere else in pycex
    # (Korbit's `isBuyerTaker`, the explicit `side` field on OKX/Bitget/Bybit),
    # so the flag inverts here.
    return Trade(
        id=str(t["id"]),
        symbol=symbol,
        side="sell" if t.get("isBuyerMaker") else "buy",
        price=float(t["price"]),
        amount=float(t["qty"]),
        timestamp=int(t.get("time", 0)),
    )


def _parse_market(d: dict[str, Any], market_type: MarketType) -> Market:
    """Parse one ``exchangeInfo`` symbol entry.

    Ticks come from ``filters[]``: ``PRICE_FILTER.tickSize`` -> ``price_tick``,
    ``LOT_SIZE.stepSize`` -> ``amount_step``, and minimum notional from
    ``NOTIONAL.minNotional`` (spot) or ``MIN_NOTIONAL.notional`` (fapi) — the two
    market types use different filter names for the same concept.
    """
    base = str(d.get("baseAsset", ""))
    quote = str(d.get("quoteAsset", ""))
    native = str(d.get("symbol", ""))
    symbol = make_linear_symbol(base, quote) if market_type == "linear" else make_spot_symbol(base, quote)
    price_tick: float | None = None
    amount_step: float | None = None
    min_notional: float | None = None
    for f in d.get("filters", []):
        ft = f.get("filterType")
        if ft == "PRICE_FILTER" and f.get("tickSize") is not None:
            price_tick = float(f["tickSize"])
        elif ft == "LOT_SIZE" and f.get("stepSize") is not None:
            amount_step = float(f["stepSize"])
        elif ft == "NOTIONAL" and f.get("minNotional") is not None:
            min_notional = float(f["minNotional"])
        elif ft == "MIN_NOTIONAL" and f.get("notional") is not None:
            min_notional = float(f["notional"])
    return Market(
        symbol=symbol,
        native=native,
        base=base,
        quote=quote,
        market_type=market_type,
        price_tick=price_tick,
        amount_step=amount_step,
        min_notional=min_notional,
        active=d.get("status") == "TRADING",
        listed_at=parse_listing_time(d.get("onboardDate")) if market_type == "linear" else None,
        raw=d,
    )


def _parse_balance(d: dict[str, Any]) -> Balance:
    entries = []
    for b in d.get("balances", []):
        free = float(b["free"])
        locked = float(b["locked"])
        if free > 0 or locked > 0:
            entries.append(BalanceEntry(asset=b["asset"], free=free, locked=locked))
    return Balance(assets=entries, raw=d)


def _parse_balance_linear(data: list[dict[str, Any]]) -> Balance:
    """Parse ``GET /fapi/v2/balance`` — a bare list, unlike spot's ``{"balances": [...]}``.

    ``locked`` has no direct field on this endpoint; it is derived as
    ``balance - availableBalance`` (funds held as position margin/unrealized loss).
    """
    entries = []
    for b in data:
        total = float(b.get("balance", 0) or 0)
        available = float(b.get("availableBalance", 0) or 0)
        if total != 0 or available != 0:
            entries.append(BalanceEntry(asset=str(b.get("asset", "")), free=available, locked=total - available))
    return Balance(assets=entries, raw=data)


def _none_if_zero(v: Any) -> float | None:
    """``0``/``"0"``/missing all mean "not meaningful" for these fields (e.g. a
    cross-margin position reports ``liquidationPrice: "0"`` when Binance can't
    compute one) — same treatment as ``_parse_order``'s ``price`` field."""
    if v in (None, ""):
        return None
    f = float(v)
    return f if f != 0 else None


def _parse_position(symbol: str, d: dict[str, Any]) -> Position:
    amt = float(d.get("positionAmt", 0) or 0)
    return Position(
        symbol=symbol,
        side="long" if amt > 0 else "short",
        amount=abs(amt),
        entry_price=_none_if_zero(d.get("entryPrice")),
        unrealized_pnl=float(d.get("unRealizedProfit", 0) or 0),
        leverage=_none_if_zero(d.get("leverage")),
        liquidation_price=_none_if_zero(d.get("liquidationPrice")),
        timestamp=int(d.get("updateTime", 0) or 0),
        raw=d,
    )


def _parse_funding(symbol: str, d: dict[str, Any]) -> FundingRate:
    return FundingRate(
        symbol=symbol,
        rate=float(d.get("lastFundingRate", 0) or 0),
        interval_hours=8,
        next_funding_time=int(d.get("nextFundingTime", 0) or 0),
        timestamp=int(d.get("time", 0) or 0),
        raw=d,
    )


def _parse_my_trade(symbol: str, t: dict[str, Any]) -> MyTrade:
    # Spot `myTrades` has no `side` field, only `isBuyer` (bool); futures
    # `userTrades` has an explicit `side` ("BUY"/"SELL") instead.
    side = str(t["side"]).lower() if "side" in t else ("buy" if t.get("isBuyer") else "sell")
    return MyTrade(
        id=str(t.get("id", "")),
        order_id=str(t.get("orderId", "")),
        symbol=symbol,
        side=side,
        price=float(t.get("price", 0) or 0),
        amount=float(t.get("qty", 0) or 0),
        fee=float(t.get("commission", 0) or 0),
        fee_asset=str(t.get("commissionAsset", "") or ""),
        timestamp=int(t.get("time", 0) or 0),
        raw=t,
    )


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    return Order(
        id=str(d.get("orderId", "")),
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("type", "").lower(),
        amount=float(d.get("origQty", 0)),
        price=float(d["price"]) if d.get("price") and float(d["price"]) > 0 else None,
        filled=float(d.get("executedQty", 0)),
        status=d.get("status", ""),
        timestamp=int(d.get("transactTime", 0) or d.get("time", 0) or d.get("updateTime", 0)),
        raw=d,
    )


# ── Errors ──

# Confirmed via developers.binance.com/docs/binance-spot-api-docs/errors (see module docstring).
_INSUFFICIENT_BALANCE_CODES = frozenset({"-2010"})
_AUTH_CODES = frozenset({"-2014", "-2015"})
_ORDER_NOT_FOUND_CODES = frozenset({"-2013"})
_RATE_LIMIT_CODES = frozenset({"-1003"})


def _map_error(code: str, msg: str) -> PyCexError:
    if code in _INSUFFICIENT_BALANCE_CODES:
        return InsufficientBalanceError(msg, code=code, exchange="binance")
    if code in _AUTH_CODES:
        return AuthenticationError(msg)
    if code in _ORDER_NOT_FOUND_CODES:
        return OrderNotFoundError(msg, code=code, exchange="binance")
    if code in _RATE_LIMIT_CODES:
        return RateLimitError(msg, code=code, exchange="binance")
    return ExchangeError(msg, code=code, exchange="binance")


def _error_mapper(status: int, data: dict[str, Any]) -> PyCexError | None:
    """Binance's wire ``code`` is a JSON number (e.g. ``-2011``), not a string
    like OKX/Bitget/Bybit's. ``_map_error`` still takes ``str`` for a uniform
    cross-adapter interface (see ``tests/test_exceptions.py``), but the raw
    numeric code is restored onto the resulting exception's ``.code`` so
    callers who compare it against the documented integer codes still see the
    type they expect (an unmapped code -> the generic ``ExchangeError``
    fallback branch, still carrying the original int)."""
    code = data.get("code")
    if code is None:
        return None
    err = _map_error(str(code), str(data.get("msg", "Unknown error")))
    if isinstance(err, ExchangeError):
        err.code = code
    return err

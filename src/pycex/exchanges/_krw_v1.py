"""Shared implementation for Upbit-compatible KRW spot REST APIs.

Upbit and Bithumb expose an (almost) identical public-market-data surface:
native symbols in ``QUOTE-BASE`` notation (e.g. ``KRW-BTC``), and the same REST
paths/response shapes for markets, candles, ticker, order book, and public
trades (``/v1/market/all``, ``/v1/candles/days``, ``/v1/candles/minutes/{u}``,
``/v1/ticker``, ``/v1/orderbook``, ``/v1/trades/ticks``). ``KrwV1Mixin`` holds
that shared surface plus the response parsers and the error-mapping helper
that both adapters' private (auth-required) methods also reuse. Each concrete
exchange (``Upbit``, ``Bithumb``) supplies its own ``__init__``, ``_headers``,
and private/trading methods — those differ enough (JWT payload shape, v1 vs
v2 endpoint paths) that sharing them would obscure more than it saves.

🚨 Daily candle day boundary differs between the two exchanges despite the
identical field shape: Upbit resets at 00:00 UTC (09:00 KST); Bithumb resets
at 00:00 KST (15:00 UTC the previous day) — see ``tests/fixtures/NOTES.md``.
``_parse_candle`` below only extracts ``candle_date_time_utc`` as the bar-open
UTC timestamp (correct for both); it does not need to know about the
boundary. Callers pulling "today's KST daily bar" must know it picks a
different underlying bar on Upbit than on Bithumb.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pycex.constants import TIMEFRAME_MS
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InsufficientBalanceError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.models.balance import Balance, BalanceEntry
from pycex.models.candle import Candle
from pycex.models.market import Market, select_markets
from pycex.models.order import Order
from pycex.models.orderbook import OrderBook, OrderBookEntry
from pycex.models.ticker import Ticker
from pycex.models.trade import Trade
from pycex.ratelimit import ExchangeRateLimiter
from pycex.symbols import parse_symbol, spot

if TYPE_CHECKING:
    from pycex.http import HTTPClient

# Canonical timeframe -> candle path suffix (`/v1/candles/{suffix}`), identical
# on both exchanges.
_TF = {"1m": "minutes/1", "5m": "minutes/5", "15m": "minutes/15", "1h": "minutes/60", "4h": "minutes/240", "1d": "days"}


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_to_ms(s: str) -> int:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class KrwV1Mixin:
    """Shared public-API surface for Upbit-compatible KRW exchanges.

    Must be mixed in *before* ``BaseExchange`` (``class X(KrwV1Mixin,
    BaseExchange)``) so the concrete adapter's own ``_http``/``name`` win
    attribute resolution where both declare them.
    """

    # Provided by BaseExchange / the concrete adapter's __init__ — declared
    # here only so mypy strict resolves attribute access inside this mixin.
    if TYPE_CHECKING:
        _http: HTTPClient
        _rate_limiter: ExchangeRateLimiter
        name: str
        _markets: dict[str, Market]

    # Symbolic `error.name` values this venue uses for auth / rate-limit failures.
    # Each concrete adapter overrides these with its own set (they differ) — see
    # `map_krw_error`.
    _auth_error_names: frozenset[str] = frozenset()
    _rate_limit_error_names: frozenset[str] = frozenset()

    def _check(self, data: Any) -> Any:
        """Raise if ``data`` is a KRW-v1 error envelope, then return it unchanged.

        🚨 Both venues can serve an error with **HTTP 200** — Bithumb always does
        (live probe 2026-08-30: ``GET /v1/ticker?markets=KRW-NOPE`` -> HTTP 200
        ``{"error":{"name":404,"message":"Code not found"}}``; Upbit serves the same
        body with HTTP 404). ``HTTPClient`` only consults its ``error_mapper`` for
        status >= 400, so on the 200 path nothing else would ever look at the
        envelope and the parsers below would blow up with ``KeyError``/
        ``AttributeError`` instead of a ``PyCexError``. Every method on this mixin
        (and every private method of both adapters) funnels its response through
        here.
        """
        if isinstance(data, dict) and "error" in data:
            exc = map_krw_error(
                200,
                data,
                exchange=self.name,
                auth_names=self._auth_error_names,
                rate_limit_names=self._rate_limit_error_names,
            )
            if exc is not None:
                raise exc
        return data

    def _format_to(self, ms: int) -> str:
        """Render the ``to`` candle query param for this venue.

        🚨 The two venues do **not** agree on this field, despite the identical
        path and response shape (live probes 2026-08-30):

        - Upbit accepts ``2026-08-25T00:00:00Z`` *and* the bare
          ``2026-08-25T00:00:00``, and reads both as **UTC**.
        - Bithumb rejects **any** timezone suffix — ``...Z`` and ``...+00:00``
          both come back as HTTP 200 ``{"error":{"name":400,"message":"Invalid
          parameter. Check the given value!"}}`` — and reads the bare form as
          **KST**. :class:`~pycex.exchanges.bithumb.Bithumb` overrides this.

        ``to`` is exclusive of the given instant on both venues.
        """
        return _iso_utc(ms)

    def to_native(self, symbol: str) -> str:
        s = parse_symbol(symbol)
        return f"{s.quote}-{s.base}"

    def from_native(self, native: str) -> str:
        quote, base = native.split("-", 1)
        return spot(base, quote)

    # ── Market Data ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="ticker"):
            data = self._check(await self._http.get("/v1/ticker", params={"markets": native}))
        return _parse_ticker(data[0])

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="orderbook"):
            data = self._check(await self._http.get("/v1/orderbook", params={"markets": native}))
        return _parse_order_book(symbol, data[0], limit)

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        params: dict[str, Any] = {"market": native, "count": min(limit, 200)}
        if until is not None:
            params["to"] = self._format_to(until + 1)  # `to` is exclusive of the given instant
        elif since is not None:
            # Both venues page backwards only (`to` is the sole cursor), so a
            # `since`-anchored page is served by asking for the window that ends
            # `limit` bars after `since`. BaseExchange.fetch_candles drives the
            # multi-page walk with `until` (candle_paging = "backward"); this
            # branch only fires for a direct single-page call.
            params["to"] = self._format_to(since + limit * TIMEFRAME_MS[timeframe])
        async with self._rate_limiter.request("query", group="candle"):
            data = self._check(await self._http.get(f"/v1/candles/{_TF[timeframe]}", params=params))
        return sorted((_parse_candle(c) for c in data), key=lambda c: c.timestamp)

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query", group="trade"):
            data = self._check(await self._http.get("/v1/trades/ticks", params={"market": native, "count": limit}))
        return [_parse_trade(symbol, t) for t in data]

    async def fetch_markets(self, *, symbols: Sequence[str] | None = None) -> list[Market]:
        async with self._rate_limiter.request("query", group="market"):
            data = self._check(await self._http.get("/v1/market/all", params={"is_details": "true"}))
        markets = [_parse_market(m) for m in data]
        self._markets = {m.native: m for m in markets}
        return select_markets(markets, symbols)


# ── Shared error mapping ──


def map_krw_error(
    status: int,
    data: dict[str, Any],
    *,
    exchange: str,
    auth_names: frozenset[str],
    rate_limit_names: frozenset[str] = frozenset(),
) -> PyCexError | None:
    """Map a ``{"error": {"name", "message"}}`` body to a ``PyCexError``.

    Both exchanges share this envelope shape, but the symbolic ``name`` values
    for auth failures differ (Upbit: ``invalid_query_payload``/``jwt_verification``/
    ``expired_access_key``/``no_authorization_ip``; Bithumb: ``jwt_verification``/
    ``expired_jwt``/``NotAllowIP``) — callers pass their own ``auth_names`` set.
    Names not recognized here (and not confirmed by a fixture) fall through to
    a generic ``ExchangeError``, per the task-6 ruling.

    🚨 ``name`` is **not always a string**: both venues answer an unknown market
    code with an integer ``name`` (live probe 2026-08-30: ``{"error":{"name":404,
    "message":"Code not found"}}`` on ``/v1/ticker``, ``/v1/orderbook``,
    ``/v1/candles/*`` and ``/v1/trades/ticks``). It is normalized with ``str()``
    before any comparison — the substring test below used to raise
    ``TypeError: argument of type 'int' is not iterable`` on exactly that payload.
    ``404`` is mapped to :class:`SymbolNotFoundError` because on this API surface
    it is what an unknown market code returns, not a wrong path.
    """
    raw_err = data.get("error")
    if raw_err is not None and not isinstance(raw_err, dict):
        return ExchangeError(str(raw_err), exchange=exchange)
    err: dict[str, Any] = raw_err or {}
    name = str(err.get("name", ""))
    message = err.get("message") or "Unknown error"
    if name == "404":
        return SymbolNotFoundError(f"{exchange}: {message}")
    if "insufficient_funds" in name:
        return InsufficientBalanceError(message, code=name, exchange=exchange)
    if name in auth_names:
        return AuthenticationError(message)
    if name == "order_not_found":
        return OrderNotFoundError(message, code=name, exchange=exchange)
    if name in rate_limit_names:
        # Both exchanges normally signal rate limiting via HTTP 429, which
        # HTTPClient already intercepts and raises RateLimitError for before
        # this mapper ever runs. This branch only fires if the same error name
        # is ever returned on a non-429 status (e.g. a 400 during a partial
        # outage).
        return RateLimitError(message, code=name, exchange=exchange)
    if name:
        return ExchangeError(message, code=name, exchange=exchange)
    return None


# ── Shared parsers ──


def _parse_candle(d: dict[str, Any]) -> Candle:
    dt = datetime.fromisoformat(d["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
    return Candle(
        timestamp=int(dt.timestamp() * 1000),
        open=float(d["opening_price"]),
        high=float(d["high_price"]),
        low=float(d["low_price"]),
        close=float(d["trade_price"]),
        volume=float(d["candle_acc_trade_volume"]),
    )


def _parse_market(d: dict[str, Any]) -> Market:
    # The market list only ever contains tradable markets — delisting means the
    # market disappears from `/v1/market/all` entirely, it does not flip a flag on a
    # market that's still listed. An investor-warning flag (`market_event.warning`
    # on Upbit, `market_warning` on Bithumb) marks warning status (still tradable),
    # not delisting, so every listed market is active; the raw flag is preserved in
    # `raw` for callers who want to surface it.
    native = d["market"]
    quote, base = native.split("-", 1)
    return Market(
        symbol=spot(base, quote),
        native=native,
        base=base,
        quote=quote,
        market_type="spot",
        active=True,
        raw=d,
    )


def _parse_ticker(d: dict[str, Any]) -> Ticker:
    quote, base = d["market"].split("-", 1)
    return Ticker(
        symbol=spot(base, quote),
        last=float(d.get("trade_price", 0) or 0),
        # Neither exchange's /v1/ticker payload carries bid/ask — that requires /v1/orderbook.
        bid=0.0,
        ask=0.0,
        high=float(d.get("high_price", 0) or 0),
        low=float(d.get("low_price", 0) or 0),
        volume=float(d.get("acc_trade_volume_24h", d.get("acc_trade_volume", 0)) or 0),
        quote_volume=float(d.get("acc_trade_price_24h", d.get("acc_trade_price", 0)) or 0),
        timestamp=int(d.get("timestamp", 0) or 0),
        raw=d,
    )


def _parse_order_book(symbol: str, d: dict[str, Any], limit: int = 20) -> OrderBook:
    units = d.get("orderbook_units", [])[:limit]
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookEntry(price=float(u["bid_price"]), amount=float(u["bid_size"])) for u in units],
        asks=[OrderBookEntry(price=float(u["ask_price"]), amount=float(u["ask_size"])) for u in units],
        timestamp=int(d.get("timestamp", 0) or 0),
        raw=d,
    )


def _parse_trade(symbol: str, d: dict[str, Any]) -> Trade:
    side = "sell" if d.get("ask_bid") == "ASK" else "buy"
    return Trade(
        id=str(d.get("sequential_id", "")),
        symbol=symbol,
        side=side,
        price=float(d.get("trade_price", 0) or 0),
        amount=float(d.get("trade_volume", 0) or 0),
        timestamp=int(d.get("timestamp", 0) or 0),
    )


def _parse_balance(result: list[Any]) -> Balance:
    entries = []
    for c in result:
        free = float(c.get("balance", 0) or 0)
        locked = float(c.get("locked", 0) or 0)
        if free > 0 or locked > 0:
            entries.append(BalanceEntry(asset=c.get("currency", ""), free=free, locked=locked))
    return Balance(assets=entries, raw=result)  # /v1/accounts returns a bare list


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    """Parse a "full" order object — the shape returned by Upbit's ``/v1/order(s)``
    and Bithumb's ``/v1/order`` and ``/v2/orders/pending``/``/v2/orders/history``
    (after Bithumb's v2 field names are normalized to v1's — see
    ``pycex.exchanges.bithumb._normalize_order_fields``).

    ``side``/``type`` are mapped only when the response actually carries
    ``side``/``ord_type`` — a sparse response (e.g. Bithumb's ``DELETE
    /v2/order``, which returns only ``order_id``/``client_order_id``/
    ``created_at``) yields ``side=""``/``type=""`` rather than a fabricated
    guess."""
    ord_type = d.get("ord_type", "")
    price_raw = d.get("price")
    price = float(price_raw) if price_raw not in (None, "") and ord_type == "limit" else None
    volume_raw = d.get("volume")
    if volume_raw not in (None, ""):
        amount = float(volume_raw)
    elif price_raw not in (None, ""):
        amount = float(price_raw)
    else:
        amount = 0.0
    created_at = d.get("created_at")
    side = {"bid": "buy", "ask": "sell"}.get(d.get("side", ""), "")
    order_type_out = "" if not ord_type else ("market" if ord_type in ("price", "market") else "limit")
    return Order(
        id=str(d.get("uuid", "")),
        symbol=symbol,
        side=side,
        type=order_type_out,
        amount=amount,
        price=price,
        filled=float(d.get("executed_volume", 0) or 0),
        status=d.get("state", ""),
        timestamp=_parse_iso_to_ms(created_at) if created_at else 0,
        raw=d,
    )

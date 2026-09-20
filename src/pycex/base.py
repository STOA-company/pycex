"""Base exchange interface — unified API that all exchange adapters implement."""

from __future__ import annotations

import asyncio
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pycex.constants import TIMEFRAME_MS
from pycex.exceptions import NotSupportedError

if TYPE_CHECKING:
    from pycex.http import HTTPClient
    from pycex.models import Balance, Candle, FundingRate, Market, MyTrade, Order, OrderBook, Position, Ticker, Trade
    from pycex.symbols import MarketType

# Public async methods that get an auto-generated `<name>_sync` twin via __init_subclass__.
_SYNC_TARGETS = (
    "fetch_ticker",
    "fetch_order_book",
    "fetch_candles",
    "fetch_trades",
    "fetch_markets",
    "fetch_balance",
    "create_order",
    "cancel_order",
    "fetch_order",
    "fetch_open_orders",
    "fetch_my_trades",
    "fetch_positions",
    "fetch_funding_rate",
    "set_leverage",
    "fetch_available_balance",
)


async def _call_and_release(self: BaseExchange, name: str, a: tuple[Any, ...], kw: dict[str, Any]) -> Any:
    """Run one async call and hand the connection pool back before the loop dies.

    ``asyncio.run`` closes the loop it created. A pool left open across that
    boundary is unusable — every later request against it raises
    ``RuntimeError: Event loop is closed`` — so the twin releases it here and
    ``HTTPClient._bind`` builds a fresh one on the next call.
    """
    try:
        return await getattr(self, name)(*a, **kw)
    finally:
        http = getattr(self, "_http", None)
        if http is not None:
            await http.close()


def _make_sync(name: str) -> Callable[..., Any]:
    def _sync(self: BaseExchange, *a: Any, **kw: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_call_and_release(self, name, a, kw))
        raise RuntimeError(f"{name}_sync called inside a running event loop; await {name}() instead")

    _sync.__name__ = f"{name}_sync"
    return _sync


class BaseExchange(ABC):
    """Abstract base class for exchange implementations.

    All exchanges expose the same unified interface. Use the concrete classes
    (Binance, Bybit, OKX, Bitget) which implement these methods. Every public
    async method listed in ``_SYNC_TARGETS`` gets a blocking ``<name>_sync``
    twin generated automatically for each subclass (see ``__init_subclass__``).
    """

    name: str = ""
    market_type: MarketType = "spot"
    sandbox: bool = False
    candle_page_limit: int = 200
    #: Which end of the range this venue's candle endpoint anchors a page on.
    #:
    #: ``"forward"`` — the page hook honours ``since`` and serves the **oldest**
    #: bars from it (Binance ``startTime``, Bybit ``start``, Bitget mix
    #: ``startTime``); ``fetch_candles`` walks the cursor up.
    #:
    #: ``"backward"`` — the endpoint anchors at ``until``/"now" and serves the
    #: **newest** bars at or before it, so a ``since`` cursor makes no progress
    #: (Upbit/Bithumb ``to``, Korbit ``end``, OKX ``after``, Bitget spot
    #: ``endTime``); ``fetch_candles`` walks the cursor down instead, passing it
    #: as ``until``. Every value is set from a live probe — see
    #: ``tests/live/test_smoke.py::test_candle_pagination``.
    candle_paging: Literal["forward", "backward"] = "forward"
    supported_timeframes: frozenset[str] = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})
    _http: HTTPClient

    if TYPE_CHECKING:
        # Declared for static typing only — the real implementations are
        # generated at runtime by __init_subclass__ (see _make_sync above).
        def fetch_ticker_sync(self, symbol: str) -> Ticker: ...
        def fetch_order_book_sync(self, symbol: str, *, limit: int = 20) -> OrderBook: ...
        def fetch_candles_sync(
            self,
            symbol: str,
            timeframe: str = "1h",
            *,
            since: int | None = None,
            until: int | None = None,
            limit: int | None = None,
        ) -> list[Candle]: ...
        def fetch_trades_sync(self, symbol: str, *, limit: int = 100) -> list[Trade]: ...
        def fetch_markets_sync(self) -> list[Market]: ...
        def fetch_balance_sync(self) -> Balance: ...
        def create_order_sync(
            self,
            symbol: str,
            side: str,
            order_type: str,
            amount: float,
            price: float | None = None,
            *,
            reduce_only: bool = False,
            client_order_id: str | None = None,
        ) -> Order: ...
        def cancel_order_sync(self, order_id: str, symbol: str) -> Order: ...
        def fetch_order_sync(
            self, order_id: str | None, symbol: str, *, client_order_id: str | None = None
        ) -> Order: ...
        def fetch_open_orders_sync(self, symbol: str | None = None) -> list[Order]: ...
        def fetch_my_trades_sync(
            self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
        ) -> list[MyTrade]: ...
        def fetch_positions_sync(self, symbols: list[str] | None = None) -> list[Position]: ...
        def fetch_funding_rate_sync(self, symbol: str) -> FundingRate: ...
        def set_leverage_sync(self, symbol: str, lever: float, mgn_mode: str = "cross") -> dict[str, Any]: ...
        def fetch_available_balance_sync(self, asset: str) -> float: ...

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        for n in _SYNC_TARGETS:
            if f"{n}_sync" not in cls.__dict__:
                setattr(cls, f"{n}_sync", _make_sync(n))

    @staticmethod
    def _resolve_sandbox(sandbox: bool, testnet: bool | None, demo: bool | None) -> bool:
        """Map the deprecated ``testnet=``/``demo=`` kwargs onto ``sandbox=``."""
        if testnet is not None or demo is not None:
            warnings.warn("testnet=/demo= are deprecated; use sandbox=", DeprecationWarning, stacklevel=3)
            return bool(sandbox or testnet or demo)
        return sandbox

    # ── Symbols ──

    @abstractmethod
    def to_native(self, symbol: str) -> str:
        """Convert a canonical symbol (``BASE/QUOTE`` or ``BASE/QUOTE:SETTLE``) to exchange notation."""
        ...

    @abstractmethod
    def from_native(self, native: str) -> str:
        """Convert an exchange-native symbol back to canonical notation."""
        ...

    # ── Market Data ──

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook: ...

    @abstractmethod
    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        """Fetch a single page of candles in exchange-native notation. Adapters implement this hook."""
        ...

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1h",
        *,
        since: int | None = None,
        until: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Fetch OHLCV candles, paginating over ``_fetch_candles_page`` when ``since``/``until`` are given.

        The page walk is driven by :attr:`candle_paging` — see that attribute and the
        two ``_walk_*`` helpers. Whichever direction the venue pages in, the result is
        deduplicated, sorted ascending, cut at ``until`` and sliced to ``limit``.
        """
        if timeframe not in self.supported_timeframes:
            raise NotSupportedError(f"{self.name} does not support timeframe {timeframe}")
        native = self.to_native(symbol)
        if since is None:
            return await self._fetch_candles_page(native, timeframe, since=None, until=until, limit=limit or 100)
        out: dict[int, Candle] = {}
        if self.candle_paging == "forward":
            await self._walk_forward(out, native, timeframe, since=since, until=until, limit=limit)
        else:
            await self._walk_backward(out, native, timeframe, since=since, until=until, limit=limit)
        result = [out[k] for k in sorted(out)]
        return result[:limit] if limit else result

    def _keep(self, page: list[Candle], out: dict[int, Candle], since: int, until: int | None) -> list[Candle]:
        """The bars of ``page`` that are in range and not already collected."""
        return [
            c
            for c in page
            if c.timestamp >= since and (until is None or c.timestamp <= until) and c.timestamp not in out
        ]

    async def _walk_forward(
        self,
        out: dict[int, Candle],
        native: str,
        timeframe: str,
        *,
        since: int,
        until: int | None,
        limit: int | None,
    ) -> None:
        """Page a ``candle_paging = "forward"`` venue: the cursor is ``since`` and walks
        *up*, to one millisecond past the newest bar of the page just served."""
        cursor = since
        while True:
            page = await self._fetch_candles_page(
                native, timeframe, since=cursor, until=until, limit=self.candle_page_limit
            )
            if not page:
                return
            new = self._keep(page, out, since, until)
            if not new:
                # No timestamp we did not already hold: end of data, or the venue
                # re-served a page we already have. Either way the walk is over.
                return
            for c in new:
                out[c.timestamp] = c
            newest = max(c.timestamp for c in page)
            if until is not None and newest >= until:
                return
            if limit is not None and len(out) >= limit:
                return
            cursor = newest + 1

    async def _walk_backward(
        self,
        out: dict[int, Candle],
        native: str,
        timeframe: str,
        *,
        since: int,
        until: int | None,
        limit: int | None,
    ) -> None:
        """Page a ``candle_paging = "backward"`` venue: the cursor is ``until`` and walks
        *down* from the upper bound (or the venue's "now") to ``since``.

        With a ``limit`` the upper bound is pulled in to ``since + limit * timeframe``:
        the caller asked for the ``limit`` **oldest** bars from ``since``, so anchoring
        at "now" would page the whole span backwards only to throw all but the tail
        away.
        """
        upper = until
        span = TIMEFRAME_MS.get(timeframe)
        if limit is not None and span is not None:
            bound = since + limit * span
            upper = bound if until is None else min(until, bound)
        cursor = upper
        while True:
            page = await self._fetch_candles_page(
                native, timeframe, since=None, until=cursor, limit=self.candle_page_limit
            )
            if not page:
                return
            new = self._keep(page, out, since, until)
            for c in new:
                out[c.timestamp] = c
            oldest = min(c.timestamp for c in page)
            if oldest <= since:
                return  # walked past the lower bound
            if not new:
                # Nothing new: the venue ignored the cursor and re-served a page we
                # already hold. Stop rather than request it forever.
                return
            cursor = oldest - 1

    @abstractmethod
    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]: ...

    @abstractmethod
    async def fetch_markets(self) -> list[Market]: ...

    # ── Account ──

    @abstractmethod
    async def fetch_balance(self) -> Balance: ...

    async def fetch_available_balance(self, asset: str) -> float:
        """How much of ``asset`` is **available to trade right now**, measured.

        🚨 This is the only honest answer to "has my last fill settled yet".
        A sell placed against a balance that has not settled comes back
        rejected — OKX ``51008``, which held a live sell off for ~70 seconds on
        2026-09-10 — and the way through is to ask the venue again, not to
        retry blindly against a local ledger.

        Therefore: every call re-measures, nothing is cached, and there is no
        retry loop here. Waiting is the caller's policy (see the settlement-
        aware execution loop, P-1).

        🚨 **This base implementation answers with `Balance.free`, which is only
        as honest as the venue's own balance endpoint.** OKX overrides it with
        the per-currency `availBal` field, which is what was actually measured
        against a live account. On every other exchange here (Binance, Bybit,
        Bitget, Upbit, Bithumb, Korbit) `free` is whatever that venue's balance
        call reports, and **nothing verifies that it excludes unsettled
        proceeds**. Do not treat it as a settlement guarantee before someone has
        measured that venue the way OKX was measured; until then, a caller who
        needs "has it settled" must confirm it against the venue itself.
        """
        balance = await self.fetch_balance()
        entry = balance.get(asset)
        return entry.free if entry else 0.0

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        raise NotSupportedError(f"{self.name}:{self.market_type} has no positions")

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        raise NotSupportedError(f"{self.name}:{self.market_type} has no funding rate")

    async def set_leverage(self, symbol: str, lever: float, mgn_mode: str = "cross") -> dict[str, Any]:
        """Set the leverage of one derivatives instrument. Returns the venue's row.

        Spot markets have no leverage, so the default raises. **No risk policy
        lives here**: a leverage cap belongs to the caller's order-budget gate,
        and a silent ceiling inside the SDK would make a caller believe its own
        cap was doing the work.
        """
        raise NotSupportedError(f"{self.name}:{self.market_type} has no leverage")

    # ── Trading ──

    @abstractmethod
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
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def fetch_order(self, order_id: str | None, symbol: str, *, client_order_id: str | None = None) -> Order: ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abstractmethod
    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]: ...

    # ── Lifecycle ──

    async def close(self) -> None:
        await self._http.close()

    def close_sync(self) -> None:
        """Blocking twin of :meth:`close`.

        Every ``*_sync`` wrapper drives the *async* client through ``asyncio.run``
        (see ``_make_sync``), so this has to close that client — it used to close a
        second, never-used ``httpx.Client``, leaving the real connection pool open
        for the whole process after ``with Exchange() as ex:``.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.close())
            return
        raise RuntimeError("close_sync called inside a running event loop; await close() instead")

    async def __aenter__(self) -> BaseExchange:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def __enter__(self) -> BaseExchange:
        return self

    def __exit__(self, *args: object) -> None:
        self.close_sync()

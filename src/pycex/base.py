"""Base exchange interface — unified API that all exchange adapters implement."""

from __future__ import annotations

import asyncio
import time
import warnings
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pycex.constants import TIMEFRAME_MS
from pycex.exceptions import NotSupportedError, RateLimitError

# One attempt plus five backoffs. Only fetch_candles_history retries.
_HISTORY_ATTEMPTS = 6

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
            closed_only: bool = False,
        ) -> list[Candle]: ...
        def fetch_trades_sync(self, symbol: str, *, limit: int = 100) -> list[Trade]: ...
        def fetch_markets_sync(self, *, symbols: Sequence[str] | None = None) -> list[Market]: ...
        def fetch_balance_sync(self) -> Balance: ...
        def create_order_sync(
            self,
            symbol: str,
            side: str,
            order_type: str,
            amount: float,
            price: float | None = None,
            *,
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
        closed_only: bool = False,
    ) -> list[Candle]:
        """Fetch OHLCV candles, paginating over ``_fetch_candles_page`` when ``since``/``until`` are given.

        The page walk is driven by :attr:`candle_paging` — see that attribute and the
        two ``_walk_*`` helpers. Whichever direction the venue pages in, the result is
        deduplicated, sorted ascending and cut at ``until``. ``closed_only`` drops the
        still-open bar before the result is sliced to ``limit``.

        ``closed_only=True`` drops a bar whose end is still ahead of now:
        keep ``timestamp + timeframe_ms <= now_ms``. ``Candle.timestamp`` is the
        bar open; adapters that receive a close time normalize it to the open
        before this check.
        """
        if timeframe not in self.supported_timeframes:
            raise NotSupportedError(f"{self.name} does not support timeframe {timeframe}")
        native = self.to_native(symbol)
        if since is None:
            # Ask for one extra bar so the still-open bar does not consume ``limit``.
            ask = limit or 100
            if closed_only and limit is not None:
                ask += 1
            page = await self._fetch_candles_page(native, timeframe, since=None, until=until, limit=ask)
            if closed_only:
                page = self._drop_open_bars(page, timeframe)
            if limit is not None:
                page = page[:limit]
            return page
        out: dict[int, Candle] = {}
        pages = (
            self._walk_forward(native, timeframe, since=since, until=until, limit=limit)
            if self.candle_paging == "forward"
            else self._walk_backward(native, timeframe, since=since, until=until, limit=limit)
        )
        async for page in pages:
            for candle in page:
                out[candle.timestamp] = candle
        result = [out[k] for k in sorted(out)]
        if closed_only:
            result = self._drop_open_bars(result, timeframe)
        if limit is not None:
            result = result[:limit]
        return result

    async def fetch_candles_history(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        until: int | None = None,
    ) -> AsyncIterator[Candle]:
        """Yield candles from ``since`` through ``until``, paging at the venue limit.

        ``until=None`` stops at the last closed bar (``timestamp + timeframe_ms <= now_ms``).
        An empty page ends the walk. ``RateLimitError`` waits for ``retry_after`` when
        the exchange set it, otherwise 1, 2, 4, 8, 16 seconds, at most five times.
        Any other error is raised on the first failure. No other method retries.

        A forward venue yields each page before the next request. A backward venue
        serves the newest page first; those pages are emitted oldest-first so the
        stream stays ascending, with no timestamp repeated from the page before.
        """
        if timeframe not in self.supported_timeframes:
            raise NotSupportedError(f"{self.name} does not support timeframe {timeframe}")
        bound = until if until is not None else int(time.time() * 1000) - TIMEFRAME_MS[timeframe]
        native = self.to_native(symbol)
        if self.candle_paging == "forward":
            async for page in self._walk_forward(native, timeframe, since=since, until=bound, limit=None, retry=True):
                for candle in page:
                    yield candle
            return
        # Newest page arrives first. An ascending stream emits the oldest page
        # first, so these pages are held until the walk reaches ``since``.
        held: list[list[Candle]] = []
        async for page in self._walk_backward(native, timeframe, since=since, until=bound, limit=None, retry=True):
            held.append(page)
        for page in reversed(held):
            for candle in page:
                yield candle

    def fetch_candles_history_sync(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        until: int | None = None,
    ) -> list[Candle]:
        """Blocking twin of :meth:`fetch_candles_history`. Returns the full list."""

        async def _collect() -> list[Candle]:
            try:
                return [c async for c in self.fetch_candles_history(symbol, timeframe, since, until)]
            finally:
                http = getattr(self, "_http", None)
                if http is not None:
                    await http.close()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_collect())
        raise RuntimeError(
            "fetch_candles_history_sync called inside a running event loop; "
            "async-iterate fetch_candles_history() instead"
        )

    def _drop_open_bars(self, candles: list[Candle], timeframe: str) -> list[Candle]:
        now_ms = int(time.time() * 1000)
        span = TIMEFRAME_MS[timeframe]
        return [c for c in candles if c.timestamp + span <= now_ms]

    def _fresh_page(self, page: list[Candle], prev: set[int], since: int, until: int | None) -> list[Candle]:
        """In-range bars not on the previous page, ascending. Dedup is that page only."""
        seen: set[int] = set()
        fresh: list[Candle] = []
        for candle in page:
            ts = candle.timestamp
            if ts in prev or ts in seen or ts < since or (until is not None and ts > until):
                continue
            seen.add(ts)
            fresh.append(candle)
        fresh.sort(key=lambda c: c.timestamp)
        return fresh

    async def _load_candle_page(
        self,
        native: str,
        timeframe: str,
        *,
        since: int | None,
        until: int | None,
        limit: int,
        retry: bool,
    ) -> list[Candle]:
        if not retry:
            return await self._fetch_candles_page(native, timeframe, since=since, until=until, limit=limit)
        last: RateLimitError | None = None
        for attempt in range(_HISTORY_ATTEMPTS):
            try:
                return await self._fetch_candles_page(native, timeframe, since=since, until=until, limit=limit)
            except RateLimitError as err:
                last = err
                if attempt == _HISTORY_ATTEMPTS - 1:
                    break
                wait = err.retry_after if err.retry_after is not None else float(2**attempt)
                await asyncio.sleep(wait)
        assert last is not None
        raise last

    async def _walk_forward(
        self,
        native: str,
        timeframe: str,
        *,
        since: int,
        until: int | None,
        limit: int | None,
        retry: bool = False,
    ) -> AsyncIterator[list[Candle]]:
        """Page a ``candle_paging = "forward"`` venue, yielding each page before the next request.

        The cursor is ``since`` and walks *up*, to one millisecond past the newest bar
        of the page just served. Dedup is the previous page's timestamps only.
        """
        cursor = since
        prev: set[int] = set()
        kept = 0
        while True:
            page = await self._load_candle_page(
                native, timeframe, since=cursor, until=until, limit=self.candle_page_limit, retry=retry
            )
            if not page:
                return
            fresh = self._fresh_page(page, prev, since, until)
            prev = {c.timestamp for c in page}
            if not fresh:
                # No timestamp the previous page did not already hold: end of data,
                # or the venue re-served a page we already have.
                return
            yield fresh
            kept += len(fresh)
            newest = max(c.timestamp for c in page)
            if until is not None and newest >= until:
                return
            if limit is not None and kept >= limit:
                return
            cursor = newest + 1

    async def _walk_backward(
        self,
        native: str,
        timeframe: str,
        *,
        since: int,
        until: int | None,
        limit: int | None,
        retry: bool = False,
    ) -> AsyncIterator[list[Candle]]:
        """Page a ``candle_paging = "backward"`` venue, yielding each page before the next request.

        The cursor is ``until`` and walks *down* from the upper bound (or the venue's
        "now") to ``since``. Dedup is the previous page's timestamps only.

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
        prev: set[int] = set()
        while True:
            page = await self._load_candle_page(
                native, timeframe, since=None, until=cursor, limit=self.candle_page_limit, retry=retry
            )
            if not page:
                return
            fresh = self._fresh_page(page, prev, since, until)
            prev = {c.timestamp for c in page}
            oldest = min(c.timestamp for c in page)
            if fresh:
                yield fresh
            if oldest <= since:
                return  # walked past the lower bound
            if not fresh:
                # Nothing new: the venue ignored the cursor and re-served a page we
                # already hold. Stop rather than request it forever.
                return
            cursor = oldest - 1

    @abstractmethod
    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]: ...

    @abstractmethod
    async def fetch_markets(self, *, symbols: Sequence[str] | None = None) -> list[Market]:
        """Market metadata; ``symbols`` (canonical, e.g. ``"BTC/KRW"``) narrows the returned list.

        ``None`` (default) returns every market. Venues answer with one catalogue request either way, so
        the filter only trims the result — except Korbit, which also skips the per-market policy calls
        of the markets left out. Symbols the venue does not list are simply absent from the result. The
        symbol cache used by ``from_native`` always covers the full catalogue.
        """

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

"""OKX V5 exchange adapter — spot (``instType=SPOT``) and USDT-margined linear
perpetual swaps (``instType=SWAP``, ``market_type="linear"``).

Both market types share the same host (``www.okx.com``) and REST surface; only
the ``instType``/``instId`` values and a handful of SWAP-only endpoints
(positions, funding rate) differ, so there is no separate base URL to switch
(unlike Binance's ``fapi.binance.com``).

Doc verification (2026-08-30):
- Live-fetched and confirmed from docs-v5:
  ``GET /api/v5/public/instruments`` — SPOT rows carry ``baseCcy``/``quoteCcy``
  (SWAP rows leave those two empty and carry ``settleCcy``/``ctValCcy``
  instead), plus ``tickSz``, ``lotSz``, ``minSz``, ``state``; confirmed by the
  recorded fixtures ``tests/fixtures/okx/{markets_spot,markets_swap}.json``.
  ``GET /api/v5/trade/fills`` (last-3-days fills) response fields
  (``tradeId, ordId, side, fillPx, fillSz, fee, feeCcy, ts, instId, instType``)
  and params (``instType`` required; ``instId``/``ordId``/``after``/``before``/
  ``limit`` optional — no timestamp-range param on this endpoint, see the note
  on ``fetch_my_trades`` below). ``POST /api/v5/trade/order`` params
  (``instId, tdMode`` [``cash``/``cross``/``isolated``], ``side, ordType, sz,
  px``, ``posSide`` only required in hedge mode).
- The docs-v5 page is one very large single-page app; the fetch tool
  truncated the "Get positions"/"Get funding rate"/"Get candlesticks history"
  sections before their field tables. Those three were instead cross-checked
  against ccxt's ``ts/src/okx.ts`` endpoint table (a well-established,
  independently maintained third-party mapping of OKX's real wire format),
  which confirms the endpoint paths verbatim: ``account/positions``,
  ``public/funding-rate``, ``market/history-candles`` (not
  ``candles-history``/``candlesticks-history`` — two wrong guesses the
  docs-fetch tool produced before this cross-check settled it). Field names
  on those three (``posSide, pos, avgPx, upl, lever, liqPx``;
  ``fundingRate, nextFundingTime``; the candle array shape) match the brief
  and the recorded ``funding``/``candles_swap_1d`` fixtures — the funding
  fixture in particular is a *real* recorded response, so its field names are
  confirmed independently of the docs-fetch truncation — but ccxt's parser
  method bodies were not readable in this pass (same truncation issue on that
  file), so treat those three as fixture/cross-reference-verified rather than
  docs-verified line-by-line.
- Not confirmed in this session: the exact error code OKX returns for a
  hedge-mode ``posSide`` mismatch on ``create_order`` (this adapter never
  sends ``posSide``, i.e. it assumes one-way mode; a hedge-mode account gets
  back whatever ``ExchangeError`` OKX raises, surfaced unchanged — same
  posture as the Binance adapter's ``-4061`` note, but here the code itself
  is not even guessed at).

``fetch_positions``/``fetch_funding_rate`` only work when ``market_type=
"linear"``; on ``"spot"`` they fall through to the shared ``BaseExchange``
default, which raises ``NotSupportedError``.

SWAP order sizing: ``sz`` on SWAP instruments is denominated in **contracts**,
not base-asset quantity (see ``ctVal``/``ctValCcy`` in ``fetch_markets``).
``create_order``/``cancel_order`` do not convert amount<->contracts in this
phase — callers pass ``sz`` directly (contracts) when trading SWAP symbols.

Inverse (coin-margined) perpetuals are out of scope in this phase — only
USDT-settled linear (``settle == quote``) is supported; ``BTC/USD:BTC``-style
canonical symbols or ``*-USD-SWAP`` native symbols raise
``SymbolNotFoundError``.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal
from urllib.parse import urlencode

from pycex.auth import okx_headers
from pycex.base import BaseExchange
from pycex.constants import CANDLE_VENUES, OKX_BASE, OKX_BROKER_ID
from pycex.exceptions import (
    AuthenticationError,
    ExchangeError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    PyCexError,
    RateLimitError,
    SettlementPendingError,
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

# 🚨 Live-verified 2026-08-30: OKX's plain "1D" bar aligns to Hong Kong time
# (UTC+8), NOT UTC midnight — `bar=1D` returns bars 8h offset from
# `bar=1Dutc` for the same instrument (confirmed by diffing the two live:
# 1D gives ts=1788019200000, 1Dutc gives ts=1788048000000, exactly 28_800_000ms
# = 8h apart; re-confirmed against the re-recorded fixture
# tests/fixtures/okx/candles_swap_1d.json, see tests/fixtures/NOTES.md). This
# library's candle contract is UTC-epoch-ms bar-open, so daily bars must
# request the "utc"-suffixed granularity instead of the bare one a naive
# reading of the docs would suggest. "1w" is left as the bare "1W" — it is
# not in BaseExchange.supported_timeframes (this library's timeframe
# contract is 1m/5m/15m/1h/4h/1d only) and its Hong-Kong-time behavior was
# not live-verified in this pass.
_TIMEFRAME_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1Dutc",
    "1w": "1W",
}


class OKX(BaseExchange):
    name = "okx"
    # history-candles caps `limit` at 100 (the recent /market/candles endpoint
    # allows up to 300, but pagination must stay within the tighter of the two
    # since either endpoint may serve a given page — see _fetch_candles_page).
    candle_page_limit = CANDLE_VENUES["okx"].page_limit
    supported_timeframes = CANDLE_VENUES["okx"].timeframes
    # `before=since` asks for records NEWER than `since` and still serves the newest
    # `limit` of them (live probe 2026-08-30: since = now-400d -> 2026-05-23..2026-08-30).
    # `after` (the upper bound) is the cursor that actually walks the history.
    candle_paging = CANDLE_VENUES["okx"].paging

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        *,
        sandbox: bool = False,
        market_type: MarketType = "spot",
        td_mode: Literal["cross", "isolated"] = "cross",
        demo: bool | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._secret = secret
        self._passphrase = passphrase
        self.market_type = market_type
        self._td_mode = td_mode
        self._inst_type = "SWAP" if market_type == "linear" else "SPOT"
        #: Measured OKX account level (``acctLv``). ``None`` until
        #: :meth:`fetch_account_config` has run — never assumed. See
        #: :meth:`_spot_td_mode`.
        self._acct_level: str | None = None
        self.sandbox = self._resolve_sandbox(sandbox, None, demo)
        self._markets: dict[str, Market] = {}
        self._rate_limiter = ExchangeRateLimiter(self.name, market_type)
        broker_headers: dict[str, str] = {}
        if OKX_BROKER_ID:
            broker_headers["broker-id"] = OKX_BROKER_ID
        self._http = HTTPClient(
            # ExchangeRateLimiter is the sole admission gate; keep HTTPClient from delaying after signing.
            OKX_BASE,
            timeout=timeout,
            rate=float("inf"),
            default_headers=broker_headers,
            error_mapper=_error_mapper,
        )

    @property
    def account_level(self) -> str | None:
        """The measured OKX account level (``acctLv``), or ``None`` if not measured yet.

        Read-only on purpose: the account level is a fact about the account,
        not something a caller may declare. A caller who "knows" it is exactly
        how the fixed ``tdMode`` bug got in.
        """
        return self._acct_level

    def to_native(self, symbol: str) -> str:
        sym = parse_symbol(symbol)
        if sym.settle:
            if sym.settle != sym.quote:
                raise SymbolNotFoundError(
                    f"okx: inverse/cross-settle perpetuals are out of scope (USDT-settled only): {symbol!r}"
                )
            return f"{sym.base}-{sym.quote}-SWAP"
        return f"{sym.base}-{sym.quote}"

    def from_native(self, native: str) -> str:
        if native in self._markets:
            return self._markets[native].symbol
        parts = native.split("-")
        if len(parts) < 2:
            raise SymbolNotFoundError(f"cannot resolve native symbol {native!r} for {self.name}")
        base, quote = parts[0], parts[1]
        if len(parts) >= 3 and parts[2] == "SWAP":
            if quote == "USD":
                raise SymbolNotFoundError(f"okx: inverse perpetual {native!r} is out of scope (USDT-settled only)")
            return f"{base}/{quote}:{quote}"
        return f"{base}/{quote}"

    def _check(self, data: dict[str, Any]) -> list[Any]:
        code = data.get("code", "0")
        if code != "0":
            raise _map_error(str(code), data.get("msg", "Unknown error"))
        result: list[Any] = data.get("data", [])
        return result

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        headers = okx_headers(self._api_key, self._secret, self._passphrase, method, path, body)
        if self.sandbox:
            headers["x-simulated-trading"] = "1"
        return headers

    # ── Market Data ──

    async def fetch_ticker(self, symbol: str) -> Ticker:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/market/ticker", params={"instId": native})
        result = self._check(data)
        if not result:
            raise ExchangeError(f"okx returned no ticker for {symbol}", code=native, exchange="okx")
        return _parse_ticker(symbol, result[0])

    async def fetch_order_book(self, symbol: str, *, limit: int = 20) -> OrderBook:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/market/books", params={"instId": native, "sz": limit})
        result = self._check(data)
        if not result:
            raise ExchangeError(f"okx returned no order book for {symbol}", code=native, exchange="okx")
        return _parse_order_book(symbol, result[0])

    async def _fetch_candles_page(
        self, native: str, timeframe: str, *, since: int | None, until: int | None, limit: int
    ) -> list[Candle]:
        # /api/v5/market/history-candles caps `limit` at 100; clamp before either
        # call (the fallback below reuses these same params verbatim) so a page
        # that falls through to history-candles never gets rejected.
        limit = min(limit, 100)
        params: dict[str, Any] = {
            "instId": native,
            "bar": _TIMEFRAME_MAP.get(timeframe, timeframe),
            "limit": str(limit),
        }
        # OKX semantics: after=ts -> records OLDER than ts; before=ts -> records NEWER
        # than ts. The two must never be sent together. When `since` is given we only
        # need a lower bound (`before`); the base class's post-page filtering already
        # trims anything past `until`. When only `until` is given (single-page, no
        # since), we need an upper bound (`after`).
        if since is not None:
            params["before"] = str(since - 1)
        elif until is not None:
            params["after"] = str(until + 1)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/market/candles", params=params)
        result = self._check(data)
        if not result:
            # The regular endpoint only serves a recent rolling window; older
            # ranges come back as an empty `data` array rather than an error.
            # Retry the identical query against history-candles, which covers
            # OKX's full candle history.
            async with self._rate_limiter.request("query"):
                data = await self._http.get("/api/v5/market/history-candles", params=params)
            result = self._check(data)
        candles = [_parse_candle(k) for k in result]
        candles.sort(key=lambda c: c.timestamp)
        return candles

    async def fetch_trades(self, symbol: str, *, limit: int = 100) -> list[Trade]:
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/market/trades", params={"instId": native, "limit": str(limit)})
        result = self._check(data)
        return [_parse_trade(symbol, t) for t in result]

    async def fetch_markets(self) -> list[Market]:
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/public/instruments", params={"instType": self._inst_type})
        result = self._check(data)
        markets = [_parse_market(d, self.market_type) for d in result]
        self._markets = {m.native: m for m in markets}
        return markets

    # ── Account ──

    async def fetch_balance(self) -> Balance:
        path = "/api/v5/account/balance"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._auth_headers("GET", path))
        result = self._check(data)
        return _parse_balance(result, data)

    async def fetch_account_config(self) -> dict[str, Any]:
        """``GET /api/v5/account/config`` — the account's own settings row.

        Caches ``acctLv`` for :meth:`_spot_td_mode`. Also carries ``posMode``
        (one-way vs hedge), ``perm`` and ``kycLv``, which callers use for a
        read-only connectivity probe.
        """
        path = "/api/v5/account/config"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, headers=self._auth_headers("GET", path))
        result = self._check(data)
        row: dict[str, Any] = result[0] if result else {}
        level = str(row.get("acctLv") or "")
        if level:
            self._acct_level = level
        return row

    async def _spot_td_mode(self) -> str:
        """The ``tdMode`` an OKX **spot** order must carry, from the measured account level.

        🚨 acctLv 1 (Simple) / 2 (Single-currency margin) take ``cash``; 3
        (Multi-currency margin) / 4 (Portfolio margin) reject ``cash`` outright
        — ``51000 Parameter tdMode error``, which wiped out four live scenarios
        on 2026-09-10 — and require ``cross``. Any other value (missing, empty,
        or a level OKX adds later) raises: an unrecognised account is a reason
        to **not send the order**, never a reason to fall back to ``cash``.
        """
        if self._acct_level is None:
            await self.fetch_account_config()
        level = self._acct_level
        if level in ("1", "2"):
            return "cash"
        if level in ("3", "4"):
            return "cross"
        raise ExchangeError(
            f"okx: cannot decide spot tdMode — account level (acctLv) is {level!r}. "
            "Measure it with fetch_account_config(); pycex will not guess 'cash'.",
            code="acctLv",
            exchange="okx",
        )

    async def fetch_available_balance(self, asset: str) -> float:
        """``GET /api/v5/account/balance?ccy=<asset>`` -> that currency's ``availBal``.

        ``availBal`` is the settled, tradable figure — deliberately **not**
        ``cashBal``/``eq``, which still count a fill that has not settled and
        would walk straight back into ``51008``. Unknown currency -> ``0.0``;
        a failed query raises rather than reporting a comfortable zero.
        """
        path = "/api/v5/account/balance"
        params = {"ccy": asset.upper()}
        full_path = f"{path}?{urlencode(params)}"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, params=params, headers=self._auth_headers("GET", full_path))
        result = self._check(data)
        for account in result:
            for detail in account.get("details", []):
                if str(detail.get("ccy", "")).upper() == asset.upper():
                    return float(detail.get("availBal", 0) or 0)
        return 0.0

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        if self.market_type != "linear":
            return await super().fetch_positions(symbols)
        path = "/api/v5/account/positions"
        query = {"instType": "SWAP"}
        full_path = f"{path}?{urlencode(query)}"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, params=query, headers=self._auth_headers("GET", full_path))
        result = self._check(data)
        positions = [
            _parse_position(self.from_native(str(p.get("instId", ""))), p)
            for p in result
            if float(p.get("pos", 0) or 0) != 0
        ]
        if symbols is not None:
            wanted = set(symbols)
            positions = [p for p in positions if p.symbol in wanted]
        return positions

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        if self.market_type != "linear":
            return await super().fetch_funding_rate(symbol)
        native = self.to_native(symbol)
        async with self._rate_limiter.request("query"):
            data = await self._http.get("/api/v5/public/funding-rate", params={"instId": native})
        result = self._check(data)
        return _parse_funding(symbol, result[0] if result else {})

    async def set_leverage(self, symbol: str, lever: float, mgn_mode: str = "cross") -> dict[str, Any]:
        """``POST /api/v5/account/set-leverage`` for one SWAP instrument.

        Rejects — before the request goes out — what OKX cannot accept: a spot
        adapter, a margin mode other than ``cross``/``isolated``, and a
        non-positive/non-finite leverage. It does **not** cap the value.
        """
        if self.market_type != "linear":
            raise NotSupportedError("okx: leverage applies to SWAP (market_type='linear') only")
        if mgn_mode not in ("cross", "isolated"):
            raise InvalidOrderError(
                f"okx: mgn_mode must be 'cross' or 'isolated', got {mgn_mode!r}", code="mgnMode", exchange="okx"
            )
        if not math.isfinite(lever) or lever <= 0:
            raise InvalidOrderError(
                f"okx: lever must be a finite positive number, got {lever!r}", code="lever", exchange="okx"
            )
        path = "/api/v5/account/set-leverage"
        body = {"instId": self.to_native(symbol), "lever": _num(lever), "mgnMode": mgn_mode}
        body_str = json.dumps(body)
        async with self._rate_limiter.request("query"):
            data = await self._http.post_raw(path, body=body_str, headers=self._auth_headers("POST", path, body_str))
        result = self._check(data)
        row: dict[str, Any] = result[0] if result else {}
        return row

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
        reduce_only: bool = False,
        tgt_ccy: str | None = None,
        tp_px: float | None = None,
        sl_px: float | None = None,
    ) -> Order:
        """Place an order.

        ``client_order_id`` is OKX's ``clOrdId`` — the venue-level idempotency
        key. pycex sends what it is given and **never generates one**: whether
        a retry reuses a key or mints a new one is the caller's policy, not the
        SDK's. OKX accepts 1-32 alphanumeric characters; anything else is
        rejected here, before the request goes out.

        ``reduce_only`` (SWAP only) marks an order that may only shrink an open
        position. ``tp_px``/``sl_px`` attach a take-profit / stop-loss to the
        order (``attachAlgoOrds``, both triggering a market exit).

        ``tgt_ccy`` names the unit of ``amount`` on a **spot market** order:
        ``"quote_ccy"`` (spend N USDT) or ``"base_ccy"`` (trade N BTC). It is
        always sent on such orders — defaulting to ``quote_ccy`` for a buy and
        ``base_ccy`` for a sell — rather than left to OKX's own default, which
        would silently turn "10 USDT worth" into "10 BTC" if the venue ever
        changed it. Every other order shape rejects it: on a limit order OKX
        ignores it, and a silently ignored parameter is worse than an error.
        """
        path = "/api/v5/trade/order"
        native = self.to_native(symbol)
        td_mode = self._td_mode if self.market_type == "linear" else await self._spot_td_mode()
        body: dict[str, Any] = {
            "instId": native,
            # SWAP takes the configured cross/isolated margin mode; SPOT's mode
            # depends on the account level — see _spot_td_mode (A-3).
            "tdMode": td_mode,
            "side": side.lower(),
            "ordType": "limit" if order_type.lower() == "limit" else "market",
            "sz": str(amount),
        }
        if client_order_id is not None:
            body["clOrdId"] = _validated_client_order_id(client_order_id)
        is_spot_market = self.market_type == "spot" and body["ordType"] == "market"
        if tgt_ccy is not None:
            if not is_spot_market:
                raise InvalidOrderError(
                    "okx: tgt_ccy applies to spot market orders only "
                    f"(market_type={self.market_type!r}, ordType={body['ordType']!r})",
                    code="tgtCcy",
                    exchange="okx",
                )
            if tgt_ccy not in ("base_ccy", "quote_ccy"):
                raise InvalidOrderError(
                    f"okx: tgt_ccy must be 'base_ccy' or 'quote_ccy', got {tgt_ccy!r}",
                    code="tgtCcy",
                    exchange="okx",
                )
            body["tgtCcy"] = tgt_ccy
        elif is_spot_market:
            body["tgtCcy"] = "quote_ccy" if body["side"] == "buy" else "base_ccy"
        if reduce_only:
            if self.market_type != "linear":
                raise InvalidOrderError(
                    "okx: reduce_only applies to SWAP (market_type='linear') only — "
                    "a spot balance has no position to reduce",
                    code="reduceOnly",
                    exchange="okx",
                )
            body["reduceOnly"] = "true"
        algo = _attached_algo_orders(tp_px, sl_px)
        if algo is not None:
            body["attachAlgoOrds"] = [algo]
        if OKX_BROKER_ID:
            body["tag"] = OKX_BROKER_ID
        if price is not None:
            body["px"] = str(price)
        # posSide is intentionally omitted: this assumes the SWAP account is in
        # one-way mode (OKX's default). A hedge-mode account requires
        # posSide="long"/"short" on every order — see module docstring.
        body_str = json.dumps(body)
        async with self._rate_limiter.request("order"):
            data = await self._http.post_raw(path, body=body_str, headers=self._auth_headers("POST", path, body_str))
        r = _check_order_response(data)
        return Order(
            id=r.get("ordId", ""),
            client_order_id=r.get("clOrdId") or client_order_id or None,
            symbol=symbol,
            side=side.lower(),
            type=order_type.lower(),
            amount=amount,
            price=price,
            raw=data,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        path = "/api/v5/trade/cancel-order"
        native = self.to_native(symbol)
        body = {"instId": native, "ordId": order_id}
        body_str = json.dumps(body)
        async with self._rate_limiter.request("order"):
            data = await self._http.post_raw(path, body=body_str, headers=self._auth_headers("POST", path, body_str))
        r = _check_order_response(data)
        return Order(
            id=r.get("ordId", order_id),
            client_order_id=r.get("clOrdId") or None,
            symbol=symbol,
            side="",
            type="",
            amount=0,
            raw=data,
        )

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        native = self.to_native(symbol)
        path = f"/api/v5/trade/order?instId={native}&ordId={order_id}"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(
                "/api/v5/trade/order",
                params={"instId": native, "ordId": order_id},
                headers=self._auth_headers("GET", path),
            )
        result = self._check(data)
        if result:
            return _parse_order(symbol, result[0])
        return Order(id=order_id, symbol=symbol, side="", type="", amount=0)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {}
        path = "/api/v5/trade/orders-pending"
        native = self.to_native(symbol) if symbol else None
        if native:
            params["instId"] = native
            path += f"?instId={native}"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(
                "/api/v5/trade/orders-pending", params=params or None, headers=self._auth_headers("GET", path)
            )
        result = self._check(data)
        return [_parse_order(self.from_native(o.get("instId", "")), o) for o in result]

    async def fetch_my_trades(
        self, symbol: str | None = None, *, since: int | None = None, limit: int | None = None
    ) -> list[MyTrade]:
        """Fetch own fills via ``GET /api/v5/trade/fills`` (last 3 days).

        That endpoint's ``after``/``before`` params page over ``billId``, not a
        timestamp, so there is no server-side way to pass ``since`` as a range
        filter here; it is instead applied client-side after parsing.
        """
        path = "/api/v5/trade/fills"
        params: dict[str, Any] = {"instType": self._inst_type}
        if symbol is not None:
            params["instId"] = self.to_native(symbol)
        if limit is not None:
            params["limit"] = str(limit)
        full_path = f"{path}?{urlencode(params)}"
        async with self._rate_limiter.request("query"):
            data = await self._http.get(path, params=params, headers=self._auth_headers("GET", full_path))
        result = self._check(data)
        trades = [
            _parse_my_trade(symbol if symbol is not None else self.from_native(str(t.get("instId", ""))), t)
            for t in result
        ]
        if since is not None:
            trades = [t for t in trades if t.timestamp >= since]
        return trades


# ── Parsers ──


def _parse_ticker(symbol: str, d: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=symbol,
        last=float(d.get("last", 0)),
        bid=float(d.get("bidPx", 0)),
        ask=float(d.get("askPx", 0)),
        high=float(d.get("high24h", 0)),
        low=float(d.get("low24h", 0)),
        volume=float(d.get("vol24h", 0)),
        quote_volume=float(d.get("volCcy24h", 0)),
        timestamp=int(d.get("ts", 0)),
        raw=d,
    )


def _parse_order_book(symbol: str, d: dict[str, Any]) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookEntry(price=float(b[0]), amount=float(b[1])) for b in d.get("bids", [])],
        asks=[OrderBookEntry(price=float(a[0]), amount=float(a[1])) for a in d.get("asks", [])],
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
        id=t.get("tradeId", ""),
        symbol=symbol,
        side=t.get("side", "").lower(),
        price=float(t.get("px", 0)),
        amount=float(t.get("sz", 0)),
        timestamp=int(t.get("ts", 0)),
    )


def _parse_market(d: dict[str, Any], market_type: MarketType) -> Market:
    """Parse one ``GET /api/v5/public/instruments`` row.

    SPOT rows carry ``baseCcy``/``quoteCcy`` directly; SWAP rows leave those
    two empty and instead carry ``ctValCcy`` (contract value currency, the
    base) and ``settleCcy`` (settlement/margin currency, the quote/settle for
    a USDT-margined linear contract). ``minSz`` is a quantity floor, not a
    notional one, so ``min_notional`` is always ``None`` for OKX.
    """
    native = str(d.get("instId", ""))
    if market_type == "linear":
        parts = native.split("-")
        base = str(d.get("ctValCcy") or (parts[0] if parts else ""))
        quote = str(d.get("settleCcy") or (parts[1] if len(parts) > 1 else ""))
        symbol = make_linear_symbol(base, quote, quote)
    else:
        base = str(d.get("baseCcy", ""))
        quote = str(d.get("quoteCcy", ""))
        symbol = make_spot_symbol(base, quote)
    tick_sz = d.get("tickSz")
    lot_sz = d.get("lotSz")
    return Market(
        symbol=symbol,
        native=native,
        base=base,
        quote=quote,
        market_type=market_type,
        price_tick=float(tick_sz) if tick_sz not in (None, "") else None,
        amount_step=float(lot_sz) if lot_sz not in (None, "") else None,
        min_notional=None,
        active=d.get("state") == "live",
        listed_at=parse_listing_time(d.get("listTime")),
        raw=d,
    )


def _parse_balance(result: list[Any], raw: dict[str, Any]) -> Balance:
    entries = []
    for account in result:
        for detail in account.get("details", []):
            free = float(detail.get("availBal", 0))
            frozen = float(detail.get("frozenBal", 0))
            if free > 0 or frozen > 0:
                entries.append(BalanceEntry(asset=detail["ccy"], free=free, locked=frozen))
    return Balance(assets=entries, raw=raw)


def _none_if_zero(v: Any) -> float | None:
    """``0``/``""``/missing all mean "not meaningful" (e.g. ``liqPx`` is ``"0"``
    when OKX can't compute a liquidation price for a low-leverage position) —
    same treatment as ``_parse_order``'s ``price`` field."""
    if v in (None, ""):
        return None
    f = float(v)
    return f if f != 0 else None


def _parse_position(symbol: str, d: dict[str, Any]) -> Position:
    """Parse one ``GET /api/v5/account/positions`` row.

    Net mode reports direction in the sign of ``pos``; hedge mode reports a
    positive ``pos`` and puts the direction in ``posSide``. Either way the
    parsed ``amount`` is absolute and ``side`` carries the direction. A zero
    position is ``"flat"`` — not a guessed direction.
    """
    pos = float(d.get("pos", 0) or 0)
    pos_side = d.get("posSide", "net")
    if pos_side in ("long", "short"):
        side = str(pos_side)
    elif pos == 0:
        side = "flat"
    else:
        side = "long" if pos > 0 else "short"
    mgn_mode = d.get("mgnMode")
    return Position(
        symbol=symbol,
        side=side,
        amount=abs(pos),
        margin_mode=str(mgn_mode) if mgn_mode else None,
        entry_price=_none_if_zero(d.get("avgPx")),
        unrealized_pnl=float(d.get("upl", 0) or 0),
        leverage=_none_if_zero(d.get("lever")),
        liquidation_price=_none_if_zero(d.get("liqPx")),
        timestamp=int(d.get("uTime", 0) or d.get("cTime", 0) or 0),
        raw=d,
    )


def _parse_funding(symbol: str, d: dict[str, Any]) -> FundingRate:
    return FundingRate(
        symbol=symbol,
        rate=float(d.get("fundingRate", 0) or 0),
        interval_hours=8,
        next_funding_time=int(d.get("nextFundingTime", 0) or 0),
        timestamp=int(d.get("ts", 0) or 0),
        raw=d,
    )


def _parse_my_trade(symbol: str, t: dict[str, Any]) -> MyTrade:
    return MyTrade(
        id=str(t.get("tradeId", "")),
        order_id=str(t.get("ordId", "")),
        symbol=symbol,
        side=str(t.get("side", "")).lower(),
        price=float(t.get("fillPx", 0) or 0),
        amount=float(t.get("fillSz", 0) or 0),
        fee=float(t.get("fee", 0) or 0),
        fee_asset=str(t.get("feeCcy", "") or ""),
        timestamp=int(t.get("ts", 0) or 0),
        raw=t,
    )


def _parse_order(symbol: str, d: dict[str, Any]) -> Order:
    return Order(
        id=d.get("ordId", ""),
        client_order_id=d.get("clOrdId") or None,
        symbol=symbol,
        side=d.get("side", "").lower(),
        type=d.get("ordType", "").lower(),
        amount=float(d.get("sz", 0)),
        price=float(d["px"]) if d.get("px") and float(d["px"]) > 0 else None,
        filled=float(d.get("accFillSz", 0)),
        status=d.get("state", ""),
        timestamp=int(d.get("cTime", 0)),
        raw=d,
    )


# ── Errors ──


def _map_error(code: str, msg: str) -> PyCexError:
    """Map OKX's ``code``/``msg`` pair (present both on HTTP>=400 bodies and on
    HTTP 200 responses with ``code != "0"``) to a ``PyCexError``."""
    if code == "51008":
        # Not "you are broke" — "not settled yet". See SettlementPendingError.
        return SettlementPendingError(msg, code=code, exchange="okx")
    if code in ("50111", "50113", "50114"):
        return AuthenticationError(msg)
    if code == "51603":
        return OrderNotFoundError(msg, code=code, exchange="okx")
    if code == "50011":
        return RateLimitError(msg, code=code, exchange="okx")
    return ExchangeError(msg, code=code, exchange="okx")


def _error_mapper(status: int, data: dict[str, Any]) -> PyCexError | None:
    code = data.get("code")
    if code is None:
        return None
    return _map_error(str(code), str(data.get("msg", "Unknown error")))


def _attached_algo_orders(tp_px: float | None, sl_px: float | None) -> dict[str, str] | None:
    """Build OKX's ``attachAlgoOrds`` entry for an attached TP/SL.

    ``tpOrdPx``/``slOrdPx`` of ``"-1"`` means "exit at market when triggered".
    A non-positive or non-finite trigger is refused rather than sent: it would
    be rejected or silently ignored, and either way the caller would believe a
    stop was in place.
    """
    algo: dict[str, str] = {}
    for label, px, trigger_key, order_key in (
        ("tp_px", tp_px, "tpTriggerPx", "tpOrdPx"),
        ("sl_px", sl_px, "slTriggerPx", "slOrdPx"),
    ):
        if px is None:
            continue
        if not math.isfinite(px) or px <= 0:
            raise InvalidOrderError(
                f"okx: {label} must be a finite positive price, got {px!r}", code=label, exchange="okx"
            )
        algo[trigger_key] = _num(px)
        algo[order_key] = "-1"
    return algo or None


def _num(value: float) -> str:
    """Render a number in OKX's canonical string form: ``3.0`` -> ``"3"``.

    Measured, so it is not overstated: the 2026-09-10 ledger shows the harness
    sending ``lever: "3.0"`` and OKX accepting it (``code 0``, echoing
    ``"3.0"``). So this is normalisation, not a rejection being avoided — it
    keeps ``lever``, trigger prices and sizes in one shape instead of letting
    Python float repr decide. Fractional values keep their digits.
    """
    return str(int(value)) if float(value).is_integer() else str(value)


#: OKX ``clOrdId``: 1-32 alphanumeric characters (letters and digits only).
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")


def _validated_client_order_id(value: str) -> str:
    """Reject a ``clOrdId`` OKX would refuse — here, not on the wire.

    Sending a malformed key and reading the rejection back is not "validation":
    the order did not go out, the caller cannot tell that apart from a venue
    outage, and an empty string silently turns idempotency **off**.
    """
    if not _CLIENT_ORDER_ID_RE.match(value):
        raise InvalidOrderError(
            f"okx: client_order_id must be 1-32 alphanumeric characters, got {value!r}",
            code="client_order_id",
            exchange="okx",
        )
    return value


def _check_order_response(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data[0]`` of an order/cancel response, raising the **per-item**
    rejection when there is one.

    🚨 Live wire shape (2026-09-10 ledger, 32 rejected orders): a rejected order
    comes back HTTP 200 with top-level ``code == "1"`` and an empty top-level
    ``msg``; the reason lives in ``data[0].sCode``/``sMsg`` (``51000 Parameter
    tdMode error``, ``51008 ...``). Reading the top-level code first — as
    ``_check`` does — throws away the only informative field and reports every
    rejection as the meaningless code ``"1"``.
    """
    rows = data.get("data") or []
    row: dict[str, Any] = rows[0] if rows else {}
    if row:
        _raise_on_scode(row)
    _check_data_code(data)
    return row


def _check_data_code(data: dict[str, Any]) -> None:
    code = str(data.get("code", "0"))
    if code != "0":
        raise _map_error(code, str(data.get("msg", "") or "Unknown error"))


def _raise_on_scode(item: dict[str, Any]) -> None:
    """Order/cancel rejections on OKX arrive as HTTP 200 + top-level ``code ==
    "0"`` (which ``_check`` treats as success) with the actual per-item
    rejection in ``data[0].sCode``/``sMsg`` instead — a batch endpoint shape
    where one order in the array can fail while others succeed. Reuses
    ``_map_error`` for the same code table (``51008`` -> insufficient balance,
    ``51603`` -> order not found, etc.)."""
    s_code = str(item.get("sCode", "0"))
    if s_code != "0":
        raise _map_error(s_code, item.get("sMsg", "Unknown error"))

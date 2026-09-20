"""Tests for the Bitget adapter: markets, fills, and the USDT-margined mix (linear) surface.

Fixtures under tests/fixtures/bitget/ are real recorded responses (Task 0):
markets_spot (GET /api/v2/spot/public/symbols), markets_linear (GET
/api/v2/mix/market/contracts, productType=USDT-FUTURES), candles_linear_1d
(GET /api/v2/mix/market/candles, ascending, index 0 = open time ms per
tests/fixtures/bitget: no NOTES.md file exists for this exchange, unlike the
brief's cross-reference to one -- shape confirmed directly from the fixture
itself: 7-element rows [ts, open, high, low, close, baseVolume, quoteVolume]),
and funding (GET /api/v2/mix/market/current-fund-rate).

Bitget's api-doc site is a client-rendered SPA that returned generic landing
content for every endpoint URL in the brief (WebFetch could not reach the
actual reference tables -- same failure mode as OKX's Task 9). Endpoint
paths, field names, response-wrapping quirks (fillList/entrustedList),
sandbox/demo mechanics, and error codes were instead cross-checked against
ccxt's ts/src/bitget.ts (fetched directly and grepped locally) -- see the
module docstring in src/pycex/exchanges/bitget.py for the full breakdown and
citations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as json_lib

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    AuthenticationError,
    InsufficientBalanceError,
    NotSupportedError,
    OrderNotFoundError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.exchanges.bitget import (
    Bitget,
    _map_error,
    _parse_candle,
    _parse_funding,
    _parse_market,
    _parse_my_trade_mix,
    _parse_order_mix,
    _parse_position,
)
from tests.conftest import load_fixture

SECRET = "s"


def _assert_valid_signature(request: httpx.Request, secret: str = SECRET) -> None:
    """Recompute HMAC-SHA256 independently (plain ``hmac``/``base64``, not the
    adapter's own ``bitget_headers``) over the real outgoing request --
    timestamp from the sent ``ACCESS-TIMESTAMP`` header, ``url.raw_path``
    (path + query exactly as sent on the wire), body from the actual request
    content -- and assert it matches. A self-referential call to
    ``bitget_headers`` would be a tautology; for POST requests this also
    catches a body that was signed as one string but sent as a
    re-serialized one (``post()``'s ``json=`` vs. ``post_raw()``'s verbatim
    ``content=``)."""
    ts = request.headers["ACCESS-TIMESTAMP"]
    method = request.method
    path = request.url.raw_path.decode()
    body = request.content.decode() if request.content else ""
    message = ts + method + path + body
    expected = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
    assert request.headers["ACCESS-SIGN"] == expected


# ── Construction / symbols ──


def test_linear_market_type_sets_usdt_futures_product_type() -> None:
    ex = Bitget(market_type="linear")
    assert ex.market_type == "linear"
    assert ex._product_type == "USDT-FUTURES"


def test_sandbox_linear_sets_susdt_futures_product_type() -> None:
    ex = Bitget(market_type="linear", sandbox=True)
    assert ex._product_type == "SUSDT-FUTURES"


def test_to_native_linear_symbol() -> None:
    ex = Bitget(market_type="linear")
    assert ex.to_native("BTC/USDT:USDT") == "BTCUSDT"


def test_to_native_sandbox_linear_symbol_gets_s_prefix() -> None:
    ex = Bitget(market_type="linear", sandbox=True)
    assert ex.to_native("BTC/USDT:USDT") == "SBTCSUSDT"


def test_from_native_linear_fallback_no_cache() -> None:
    ex = Bitget(market_type="linear")
    assert ex.from_native("BTCUSDT") == "BTC/USDT:USDT"


def test_from_native_sandbox_linear_fallback_strips_s_prefix() -> None:
    ex = Bitget(market_type="linear", sandbox=True)
    assert ex.from_native("SBTCSUSDT") == "BTC/USDT:USDT"


def test_symbol_round_trip_linear() -> None:
    ex = Bitget(market_type="linear")
    native = ex.to_native("ETH/USDT:USDT")
    assert ex.from_native(native) == "ETH/USDT:USDT"


def test_to_native_inverse_settle_rejected() -> None:
    ex = Bitget(market_type="linear")
    with pytest.raises(SymbolNotFoundError):
        ex.to_native("BTC/USD:BTC")


def test_from_native_spot_fallback_no_cache() -> None:
    ex = Bitget()
    assert ex.from_native("BTCUSDT") == "BTC/USDT"


# ── fetch_markets parsing ──


def test_parse_market_spot_precision_and_min_notional() -> None:
    raw = load_fixture("bitget", "markets_spot")["data"][0]
    m = _parse_market(raw, "spot")
    assert m.symbol == "BTC/USDT"
    assert m.native == "BTCUSDT"
    assert m.base == "BTC" and m.quote == "USDT"
    assert m.market_type == "spot"
    assert m.price_tick == 10**-2
    assert m.amount_step == 10**-6
    assert m.min_notional == 1.0
    assert m.active is True


def test_parse_market_linear_price_place_and_size_multiplier() -> None:
    raw = load_fixture("bitget", "markets_linear")["data"][0]
    m = _parse_market(raw, "linear")
    assert m.symbol == "BTC/USDT:USDT"
    assert m.native == "BTCUSDT"
    assert m.base == "BTC" and m.quote == "USDT"
    assert m.market_type == "linear"
    assert m.price_tick == 10**-1
    assert m.amount_step == 0.0001
    assert m.min_notional == 5.0
    assert m.active is True


async def test_fetch_markets_spot_populates_cache(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bitget", "markets_spot"))
    ex = Bitget()
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/spot/public/symbols"
    assert len(markets) == 1
    assert ex.from_native("BTCUSDT") == "BTC/USDT"
    await ex.close()


async def test_fetch_markets_linear_populates_cache_and_sends_product_type(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bitget", "markets_linear"))
    ex = Bitget(market_type="linear")
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/market/contracts"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert markets[0].symbol == "BTC/USDT:USDT"
    assert ex.from_native("BTCUSDT") == "BTC/USDT:USDT"
    await ex.close()


# ── candles ──


def test_parse_candle_mix_array_shape() -> None:
    raw = load_fixture("bitget", "candles_linear_1d")["data"][0]
    c = _parse_candle(raw)
    assert c.timestamp == int(raw[0])
    assert c.open == float(raw[1])
    assert c.volume == float(raw[5])
    # Recorded with granularity=1Dutc (Task 13 fix) — must land on UTC
    # midnight, not the Hong Kong midnight the bare granularity=1D/1day
    # produces (offset 57_600_000).
    assert c.timestamp % 86_400_000 == 0


async def test_fetch_candles_page_spot_daily_uses_utc_suffixed_granularity(httpx_mock: HTTPXMock) -> None:
    """🚨 Live-verified 2026-08-30 (Task 13): Bitget spot's bare `granularity=
    1day` aligns to Hong Kong time (UTC+8), not UTC midnight — same trap as
    OKX's bare `bar=1D`. `granularity=1Dutc` is required to match this
    library's UTC-epoch-ms bar-open contract. Reuses the mix candle fixture —
    the array shape's first six fields (ts/open/high/low/close/volume) are
    identical between spot and mix."""
    httpx_mock.add_response(json=load_fixture("bitget", "candles_linear_1d"))
    ex = Bitget(market_type="spot")
    await ex._fetch_candles_page("BTCUSDT", "1d", since=None, until=None, limit=100)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/spot/market/candles"
    assert req.url.params["granularity"] == "1Dutc"
    await ex.close()


async def test_fetch_candles_page_spot_rtoken_falls_back_from_utc_daily_to_1day(httpx_mock: HTTPXMock) -> None:
    """Live-verified 2026-09-20: Bitget Reality stock (rtoken) spot pairs
    (``areaSymbol=yes``, base ``rAAPL`` / native ``RAAPLUSDT``) reject
    ``granularity=1Dutc`` with HTTP 400 ``code=48001``
    ``Parameter validation failed null``. The same symbol returns bars with
    ``granularity=1day``. Crypto spot must still try ``1Dutc`` first.
    """
    httpx_mock.add_response(
        status_code=400,
        json={"code": "48001", "msg": "Parameter validation failed null", "data": None},
    )
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "data": [["1789920000000", "333.99", "335.12", "333.99", "335.1", "1.124", "375.992052"]],
        }
    )
    ex = Bitget(market_type="spot")
    candles = await ex._fetch_candles_page("RAAPLUSDT", "1d", since=None, until=None, limit=2)
    reqs = httpx_mock.get_requests()
    assert [r.url.params["granularity"] for r in reqs] == ["1Dutc", "1day"]
    assert reqs[0].url.path == reqs[1].url.path == "/api/v2/spot/market/candles"
    assert len(candles) == 1
    assert candles[0].timestamp == 1789920000000
    assert candles[0].open == 333.99
    await ex.close()


async def test_fetch_candles_page_spot_rtoken_remembers_1day_after_48001(httpx_mock: HTTPXMock) -> None:
    """After 1Dutc is rejected once, later pages for that native must not
    keep paying the 48001 round-trip."""
    candle = {
        "code": "00000",
        "msg": "success",
        "data": [["1789920000000", "333.99", "335.12", "333.99", "335.1", "1.124", "375.992052"]],
    }
    httpx_mock.add_response(
        status_code=400,
        json={"code": "48001", "msg": "Parameter validation failed null", "data": None},
    )
    httpx_mock.add_response(json=candle)
    httpx_mock.add_response(json=candle)
    ex = Bitget(market_type="spot")
    await ex._fetch_candles_page("RAAPLUSDT", "1d", since=None, until=None, limit=2)
    await ex._fetch_candles_page("RAAPLUSDT", "1d", since=None, until=1_789_920_000_000, limit=2)
    assert [r.url.params["granularity"] for r in httpx_mock.get_requests()] == ["1Dutc", "1day", "1day"]
    await ex.close()


async def test_fetch_candles_page_linear_sends_product_type_granularity_and_range(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bitget", "candles_linear_1d"))
    ex = Bitget(market_type="linear")
    candles = await ex._fetch_candles_page("BTCUSDT", "1d", since=1787875200000, until=1788048000000, limit=1000)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/market/candles"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert req.url.params["granularity"] == "1Dutc"
    assert req.url.params["startTime"] == "1787875200000"
    assert req.url.params["endTime"] == "1788048000000"
    # limit clamped to 200 (the tighter of "recent" (1000) vs "history" (200) caps)
    assert req.url.params["limit"] == "200"
    assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)
    await ex.close()


async def test_fetch_candles_page_linear_history_fallback_on_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": []})
    httpx_mock.add_response(json=load_fixture("bitget", "candles_linear_1d"))
    ex = Bitget(market_type="linear")
    candles = await ex._fetch_candles_page("BTCUSDT", "1d", since=None, until=None, limit=200)
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert reqs[0].url.path == "/api/v2/mix/market/candles"
    assert reqs[1].url.path == "/api/v2/mix/market/history-candles"
    assert len(candles) == 3
    await ex.close()


# ── funding rate ──


def test_parse_funding() -> None:
    raw = load_fixture("bitget", "funding")["data"][0]
    f = _parse_funding("BTC/USDT:USDT", raw)
    assert f.rate == float(raw["fundingRate"])
    assert f.interval_hours == int(raw["fundingRateInterval"])
    assert f.next_funding_time == int(raw["nextUpdate"])


async def test_fetch_funding_rate_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("bitget", "funding"))
    ex = Bitget(market_type="linear")
    fr = await ex.fetch_funding_rate("BTC/USDT:USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/market/current-fund-rate"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert fr.symbol == "BTC/USDT:USDT"
    assert fr.rate == float(load_fixture("bitget", "funding")["data"][0]["fundingRate"])
    await ex.close()


async def test_fetch_funding_rate_spot_not_supported() -> None:
    ex = Bitget()
    with pytest.raises(NotSupportedError):
        await ex.fetch_funding_rate("BTC/USDT")
    await ex.close()


# ── positions ──


async def test_fetch_positions_spot_not_supported() -> None:
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p")
    with pytest.raises(NotSupportedError):
        await ex.fetch_positions()
    await ex.close()


def test_parse_position_hold_side_and_zero_liquidation_price_is_none() -> None:
    long_pos = _parse_position(
        "BTC/USDT:USDT",
        {
            "holdSide": "long",
            "total": "0.002",
            "openPriceAvg": "37355.5",
            "unrealizedPL": "0.03",
            "leverage": "20",
            "liquidationPrice": "31725.02",
            "cTime": "1700807507275",
        },
    )
    assert long_pos.side == "long"
    assert long_pos.amount == 0.002
    assert long_pos.entry_price == 37355.5
    assert long_pos.liquidation_price == 31725.02

    short_pos = _parse_position(
        "ETH/USDT:USDT",
        {
            "holdSide": "short",
            "total": "3",
            "openPriceAvg": "3000.0",
            "unrealizedPL": "-10.0",
            "leverage": "5",
            "liquidationPrice": "0",
            "cTime": "1700807507275",
        },
    )
    assert short_pos.side == "short"
    assert short_pos.liquidation_price is None  # "0" -> None, not 0.0


async def test_fetch_positions_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "requestTime": 1700807531673,
            "data": [
                {
                    "marginCoin": "USDT",
                    "symbol": "BTCUSDT",
                    "holdSide": "long",
                    "total": "0.002",
                    "openPriceAvg": "37355.5",
                    "unrealizedPL": "0.03",
                    "leverage": "20",
                    "liquidationPrice": "31725.02",
                    "cTime": "1700807507275",
                },
                {
                    "marginCoin": "USDT",
                    "symbol": "ETHUSDT",
                    "holdSide": "short",
                    "total": "0",
                    "openPriceAvg": "0",
                    "unrealizedPL": "0",
                    "leverage": "20",
                    "liquidationPrice": "0",
                    "cTime": "0",
                },
            ],
        }
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    positions = await ex.fetch_positions()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/position/all-position"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert req.url.params["marginCoin"] == "USDT"
    _assert_valid_signature(req)
    assert req.headers["ACCESS-KEY"] == "k"
    assert req.headers["ACCESS-PASSPHRASE"] == "p"
    assert len(positions) == 1  # flat ETH (total=0) excluded
    assert positions[0].symbol == "BTC/USDT:USDT"
    assert positions[0].side == "long"
    await ex.close()


async def test_paptrading_header_only_for_spot_sandbox_not_linear(httpx_mock: HTTPXMock) -> None:
    """Per the module docstring: Bitget's dedicated SUSDT-FUTURES productType
    already signals demo mode for linear, so the paptrading header is only
    added for spot sandbox (not doubled up for linear sandbox)."""
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": []})
    spot_sandbox = Bitget(api_key="k", secret=SECRET, passphrase="p", sandbox=True)
    await spot_sandbox.fetch_balance()
    spot_req = httpx_mock.get_requests()[-1]
    assert spot_req.headers["paptrading"] == "1"
    await spot_sandbox.close()

    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": []})
    linear_sandbox = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear", sandbox=True)
    await linear_sandbox.fetch_positions()
    linear_req = httpx_mock.get_requests()[-1]
    assert "paptrading" not in linear_req.headers
    assert linear_req.url.params["productType"] == "SUSDT-FUTURES"
    await linear_sandbox.close()


# ── balance ──


async def test_fetch_balance_linear_signed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "requestTime": 1700625127294,
            "data": [{"marginCoin": "USDT", "locked": "0", "available": "12.5"}],
        }
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/account/accounts"
    assert req.url.params["productType"] == "USDT-FUTURES"
    _assert_valid_signature(req)
    entry = balance.get("USDT")
    assert entry is not None
    assert entry.free == 12.5
    await ex.close()


# ── orders (mix): status/state field fallback, signed GET requests ──


def test_parse_order_mix_reads_status_field_from_orders_pending() -> None:
    """/mix/order/orders-pending (entrustedList) rows use `status`, not `state`."""
    o = _parse_order_mix(
        "BTC/USDT:USDT",
        {
            "symbol": "BTCUSDT",
            "orderId": "1111488897767604224",
            "size": "0.002",
            "price": "25000",
            "baseVolume": "0",
            "side": "buy",
            "orderType": "limit",
            "status": "live",
            "cTime": "1700725524378",
        },
    )
    assert o.status == "live"


def test_parse_order_mix_falls_back_to_state_field_from_order_detail() -> None:
    """/mix/order/detail rows use `state`, not `status`."""
    o = _parse_order_mix(
        "BTC/USDT:USDT",
        {
            "symbol": "BTCUSDT",
            "orderId": "1111465253393825792",
            "size": "0.001",
            "price": "27000",
            "baseVolume": "0",
            "side": "buy",
            "orderType": "limit",
            "state": "live",
            "cTime": "1700719887120",
        },
    )
    assert o.status == "live"


async def test_fetch_open_orders_linear_reads_status_from_entrustedlist(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "requestTime": 1700725609065,
            "data": {
                "entrustedList": [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": "1111488897767604224",
                        "size": "0.002",
                        "price": "25000",
                        "baseVolume": "0",
                        "side": "buy",
                        "orderType": "limit",
                        "status": "live",
                        "cTime": "1700725524378",
                    }
                ],
                "endId": "1111488897767604224",
            },
        }
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    orders = await ex.fetch_open_orders("BTC/USDT:USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/order/orders-pending"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert req.url.params["symbol"] == "BTCUSDT"
    _assert_valid_signature(req)
    assert req.headers["ACCESS-KEY"] == "k"
    assert len(orders) == 1
    assert orders[0].symbol == "BTC/USDT:USDT"
    assert orders[0].status == "live"
    await ex.close()


async def test_fetch_order_linear_reads_state_from_order_detail(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "requestTime": 1700719918781,
            "data": {
                "symbol": "BTCUSDT",
                "size": "0.001",
                "orderId": "1111465253393825792",
                "price": "27000",
                "baseVolume": "0",
                "side": "buy",
                "orderType": "limit",
                "state": "live",
                "cTime": "1700719887120",
            },
        }
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    order = await ex.fetch_order("1111465253393825792", "BTC/USDT:USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/order/detail"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["orderId"] == "1111465253393825792"
    _assert_valid_signature(req)
    assert req.headers["ACCESS-KEY"] == "k"
    assert order.symbol == "BTC/USDT:USDT"
    assert order.status == "live"
    await ex.close()


# ── my trades (fills) ──


def test_parse_my_trade_mix() -> None:
    t = _parse_my_trade_mix(
        "BTC/USDT:USDT",
        {
            "tradeId": "1111468664328269825",
            "symbol": "BTCUSDT",
            "orderId": "1111468664264753162",
            "price": "37271.4",
            "baseVolume": "0.001",
            "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.02236284"}],
            "side": "buy",
            "cTime": "1700720700342",
        },
    )
    assert t.id == "1111468664328269825"
    assert t.order_id == "1111468664264753162"
    assert t.side == "buy"
    assert t.price == 37271.4
    assert t.amount == 0.001
    assert t.fee == -0.02236284
    assert t.fee_asset == "USDT"


async def test_fetch_my_trades_linear_unwraps_filllist(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "00000",
            "msg": "success",
            "requestTime": 1700803357487,
            "data": {
                "fillList": [
                    {
                        "tradeId": "1111468664328269825",
                        "symbol": "BTCUSDT",
                        "orderId": "1111468664264753162",
                        "price": "37271.4",
                        "baseVolume": "0.001",
                        "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.02236284"}],
                        "side": "buy",
                        "cTime": "1700720700342",
                    }
                ],
                "endId": "1099351587643699201",
            },
        }
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    trades = await ex.fetch_my_trades("BTC/USDT:USDT", limit=50)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/order/fills"
    assert req.url.params["productType"] == "USDT-FUTURES"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["limit"] == "50"
    _assert_valid_signature(req)
    assert len(trades) == 1
    assert trades[0].symbol == "BTC/USDT:USDT"
    await ex.close()


async def test_fetch_my_trades_requires_symbol() -> None:
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    with pytest.raises(ValueError):
        await ex.fetch_my_trades()
    await ex.close()


# ── create_order / cancel_order: mix body shape, signed-body-matches-wire ──


async def test_create_order_linear_body_shape(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"code": "00000", "msg": "success", "requestTime": 1, "data": {"orderId": "1", "clientOid": "c"}}
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    order = await ex.create_order("BTC/USDT:USDT", "buy", "limit", 0.01, 50000.0)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v2/mix/order/place-order"
    body = json_lib.loads(req.content.decode())
    assert body["symbol"] == "BTCUSDT"
    assert body["productType"] == "USDT-FUTURES"
    assert body["marginMode"] == "crossed"
    assert body["marginCoin"] == "USDT"
    assert body["side"] == "buy"
    assert body["orderType"] == "limit"
    assert body["force"] == "gtc"
    assert "tradeSide" not in body  # one-way mode
    assert order.id == "1"
    assert order.amount == 0.01
    assert order.price == 50000.0
    await ex.close()


async def test_create_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    """The prehash must cover the exact bytes sent on the wire, not a dict that
    gets re-serialized separately by httpx -- otherwise every real order
    would fail Bitget's signature check. Recomputes independently from the
    real captured request rather than calling the adapter's own signer."""
    httpx_mock.add_response(
        json={"code": "00000", "msg": "success", "requestTime": 1, "data": {"orderId": "1", "clientOid": "c"}}
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    await ex.create_order("BTC/USDT:USDT", "buy", "limit", 0.01, 50000.0)
    req = httpx_mock.get_request()
    assert req.headers["Content-Type"] == "application/json"
    _assert_valid_signature(req)
    await ex.close()


async def test_cancel_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"code": "00000", "msg": "success", "requestTime": 1, "data": {"orderId": "1", "clientOid": "c"}}
    )
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p", market_type="linear")
    order = await ex.cancel_order("1", "BTC/USDT:USDT")
    req = httpx_mock.get_request()
    body = json_lib.loads(req.content.decode())
    assert body["productType"] == "USDT-FUTURES"
    assert order.side == ""
    assert order.type == ""
    _assert_valid_signature(req)
    await ex.close()


# ── error mapping ──


async def test_map_error_insufficient_balance(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "43012", "msg": "Insufficient balance", "requestTime": 1, "data": None})
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p")
    with pytest.raises(InsufficientBalanceError):
        await ex.fetch_balance()
    await ex.close()


def test_map_error_authentication() -> None:
    err = _map_error("40009", "sign signature error")
    assert isinstance(err, AuthenticationError)


def test_map_error_order_not_found() -> None:
    err = _map_error("43001", "Order does not exist")
    assert isinstance(err, OrderNotFoundError)


def test_map_error_rate_limit() -> None:
    err = _map_error("1001", "The request is too frequent")
    assert isinstance(err, RateLimitError)


async def test_map_error_on_http_400_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"code": "40006", "msg": "Invalid ACCESS_KEY"})
    ex = Bitget(api_key="k", secret=SECRET, passphrase="p")
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    await ex.close()


async def test_fetch_candles_page_spot_sorts_ascending(httpx_mock: HTTPXMock) -> None:
    """The mix branch sorted its page; the spot branch did not, so the ascending
    contract held or not depending on which market type the caller picked."""
    rows = [
        ["1788048000000", "3", "3", "3", "3", "1", "1"],
        ["1787961600000", "2", "2", "2", "2", "1", "1"],
        ["1787875200000", "1", "1", "1", "1", "1", "1"],
    ]
    httpx_mock.add_response(json={"code": "00000", "msg": "success", "data": rows})
    ex = Bitget()
    candles = await ex._fetch_candles_page("BTCUSDT", "1d", since=None, until=None, limit=3)
    assert [c.timestamp for c in candles] == [1787875200000, 1787961600000, 1788048000000]
    await ex.close()

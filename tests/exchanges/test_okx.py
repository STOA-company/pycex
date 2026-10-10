"""Tests for the OKX adapter: markets, fills, and the USDT-margined SWAP surface.

Fixtures under tests/fixtures/okx/ are real recorded responses (Task 0):
markets_spot/markets_swap (GET /api/v5/public/instruments, BTC-USDT /
BTC-USDT-SWAP), candles_swap_1d (GET /api/v5/market/candles, newest-first,
index 0 = open time ms per tests/fixtures/NOTES.md), and funding (GET
/api/v5/public/funding-rate, BTC-USDT-SWAP). Endpoint/field shapes for
instruments, fills, and order params were live-fetched and confirmed against
docs-v5 (2026-08-30); positions/funding-rate/history-candles endpoint paths
were cross-checked against ccxt's okx.ts endpoint table after the docs-v5
single-page fetch truncated before those sections — see the module docstring
in src/pycex/exchanges/okx.py for the full breakdown.
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
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    RateLimitError,
    SymbolNotFoundError,
)
from pycex.exchanges.okx import (
    OKX,
    _map_error,
    _parse_candle,
    _parse_funding,
    _parse_market,
    _parse_my_trade,
    _parse_position,
)
from tests.conftest import load_fixture

SECRET = "s"

#: A/3: 현물 주문은 acctLv 를 실측한 뒤에만 나간다. 계좌 등급이 주제가 아닌
#: 테스트는 이 응답을 먼저 물려 두거나 _spot_with_known_account_level() 을 쓴다.
_ACCT_CONFIG = {"code": "0", "msg": "", "data": [{"acctLv": "1", "posMode": "net_mode"}]}


def _spot_with_known_account_level() -> OKX:
    """등급을 이미 실측해 둔 현물 어댑터(추가 요청 없이 주문 한 건만 나간다)."""
    ex = OKX(api_key="k", secret="s", passphrase="p")
    ex._acct_level = "1"
    return ex


def _assert_valid_signature(request: httpx.Request, secret: str = SECRET) -> None:
    """Recompute HMAC-SHA256 independently (plain ``hmac``/``base64``, not the
    adapter's own ``okx_headers``) over the real outgoing request — timestamp
    from the sent ``OK-ACCESS-TIMESTAMP`` header, ``url.raw_path`` (path +
    query, exactly as sent on the wire), body from the actual request
    content — and assert it matches. A self-referential call to
    ``okx_headers`` would be a tautology; for POST requests this also catches
    a body that was signed as one string but sent as a re-serialized one
    (e.g. ``post()``'s ``json=`` vs. ``post_raw()``'s verbatim ``content=``).
    """
    ts = request.headers["OK-ACCESS-TIMESTAMP"]
    method = request.method
    path = request.url.raw_path.decode()
    body = request.content.decode() if request.content else ""
    message = ts + method + path + body
    expected = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
    assert request.headers["OK-ACCESS-SIGN"] == expected


# ── Construction / symbols ──


def test_linear_market_type_sets_swap_inst_type() -> None:
    ex = OKX(market_type="linear")
    assert ex.market_type == "linear"
    assert ex._inst_type == "SWAP"


def test_spot_market_type_sets_spot_inst_type() -> None:
    ex = OKX()
    assert ex._inst_type == "SPOT"


def test_td_mode_default_cross() -> None:
    ex = OKX(market_type="linear")
    assert ex._td_mode == "cross"


def test_td_mode_isolated() -> None:
    ex = OKX(market_type="linear", td_mode="isolated")
    assert ex._td_mode == "isolated"


def test_to_native_linear_symbol() -> None:
    ex = OKX(market_type="linear")
    assert ex.to_native("BTC/USDT:USDT") == "BTC-USDT-SWAP"


def test_from_native_linear_fallback_no_cache() -> None:
    ex = OKX(market_type="linear")
    assert ex.from_native("BTC-USDT-SWAP") == "BTC/USDT:USDT"


def test_from_native_spot_fallback_no_cache() -> None:
    ex = OKX()
    assert ex.from_native("BTC-USDT") == "BTC/USDT"


def test_to_native_inverse_settle_rejected() -> None:
    ex = OKX(market_type="linear")
    with pytest.raises(SymbolNotFoundError):
        ex.to_native("BTC/USD:BTC")


def test_from_native_inverse_swap_rejected() -> None:
    ex = OKX(market_type="linear")
    with pytest.raises(SymbolNotFoundError):
        ex.from_native("BTC-USD-SWAP")


# ── fetch_markets parsing ──


def test_parse_market_spot_ticks_floats() -> None:
    raw = load_fixture("okx", "markets_spot")["data"][0]
    m = _parse_market(raw, "spot")
    assert m.symbol == "BTC/USDT"
    assert m.native == "BTC-USDT"
    assert m.base == "BTC" and m.quote == "USDT"
    assert m.market_type == "spot"
    assert m.price_tick == 0.1
    assert m.amount_step == 0.00000001
    assert m.min_notional is None
    assert m.active is True


def test_parse_market_swap_derives_base_quote_from_ctval_settle() -> None:
    raw = load_fixture("okx", "markets_swap")["data"][0]
    m = _parse_market(raw, "linear")
    assert m.symbol == "BTC/USDT:USDT"
    assert m.native == "BTC-USDT-SWAP"
    assert m.base == "BTC" and m.quote == "USDT"
    assert m.market_type == "linear"
    assert m.price_tick == 0.1
    assert m.amount_step == 0.01
    assert m.min_notional is None
    assert m.active is True


async def test_fetch_markets_spot_populates_cache(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("okx", "markets_spot"))
    ex = OKX()
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/public/instruments"
    assert req.url.params["instType"] == "SPOT"
    assert len(markets) == 1
    assert ex.from_native("BTC-USDT") == "BTC/USDT"
    await ex.close()


async def test_fetch_markets_linear_populates_cache_and_symbol(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("okx", "markets_swap"))
    ex = OKX(market_type="linear")
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/public/instruments"
    assert req.url.params["instType"] == "SWAP"
    assert markets[0].symbol == "BTC/USDT:USDT"
    assert ex.from_native("BTC-USDT-SWAP") == "BTC/USDT:USDT"
    await ex.close()


# ── candles: parsing, cursor, history fallback ──


def test_parse_candle_swap_array_shape() -> None:
    raw = load_fixture("okx", "candles_swap_1d")["data"][0]
    c = _parse_candle(raw)
    assert c.timestamp == int(raw[0])
    assert c.open == float(raw[1])
    assert c.volume == float(raw[5])
    # Recorded with bar=1Dutc (Task 13 fix) — must land on UTC midnight, not
    # the Hong Kong midnight the bare bar=1D produces (offset 57_600_000).
    assert c.timestamp % 86_400_000 == 0


async def test_fetch_candles_page_linear_sorts_ascending(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("okx", "candles_swap_1d"))
    ex = OKX(market_type="linear")
    candles = await ex._fetch_candles_page("BTC-USDT-SWAP", "1d", since=None, until=None, limit=100)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/market/candles"
    assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)
    assert candles[0].timestamp == 1787875200000  # oldest of the three fixture rows
    assert candles[-1].timestamp == 1788048000000  # newest
    await ex.close()


async def test_fetch_candles_page_daily_uses_utc_suffixed_bar(httpx_mock: HTTPXMock) -> None:
    """🚨 Live-verified 2026-08-30 (Task 13): OKX's bare `bar=1D` aligns to Hong
    Kong time (UTC+8), not UTC midnight — `bar=1Dutc` is required to match this
    library's UTC-epoch-ms bar-open contract. Same trap applies to spot, so no
    market_type branch here."""
    httpx_mock.add_response(json=load_fixture("okx", "candles_swap_1d"))
    ex = OKX(market_type="linear")
    await ex._fetch_candles_page("BTC-USDT-SWAP", "1d", since=None, until=None, limit=100)
    req = httpx_mock.get_request()
    assert req.url.params["bar"] == "1Dutc"
    await ex.close()


async def test_candles_history_fallback_on_empty_regular_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    httpx_mock.add_response(json=load_fixture("okx", "candles_swap_1d"))
    ex = OKX(market_type="linear")
    since = 1787875200000
    candles = await ex._fetch_candles_page("BTC-USDT-SWAP", "1d", since=since, until=None, limit=100)
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert reqs[0].url.path == "/api/v5/market/candles"
    assert reqs[1].url.path == "/api/v5/market/history-candles"
    assert reqs[0].url.params.get("before") == str(since - 1)
    assert reqs[1].url.params.get("before") == str(since - 1)
    assert len(candles) == 3
    assert candles == sorted(candles, key=lambda c: c.timestamp)
    await ex.close()


async def test_fetch_candles_page_clamps_limit_to_100(httpx_mock: HTTPXMock) -> None:
    """history-candles caps limit at 100; a caller-requested 200 must be
    clamped before either request goes out (OKX.candle_page_limit = 100)."""
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    ex = OKX(market_type="linear")
    assert ex.candle_page_limit == 100
    await ex._fetch_candles_page("BTC-USDT-SWAP", "1d", since=1787875200000, until=None, limit=200)
    reqs = httpx_mock.get_requests()
    assert reqs[0].url.params["limit"] == "100"
    assert reqs[1].url.params["limit"] == "100"
    await ex.close()


# ── funding rate ──


def test_parse_funding() -> None:
    raw = load_fixture("okx", "funding")["data"][0]
    f = _parse_funding("BTC/USDT:USDT", raw)
    assert f.rate == float(raw["fundingRate"])
    assert f.next_funding_time == int(raw["nextFundingTime"])
    assert f.interval_hours == 8


async def test_fetch_funding_rate_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("okx", "funding"))
    ex = OKX(market_type="linear")
    fr = await ex.fetch_funding_rate("BTC/USDT:USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/public/funding-rate"
    assert req.url.params["instId"] == "BTC-USDT-SWAP"
    assert fr.symbol == "BTC/USDT:USDT"
    assert fr.rate == float(load_fixture("okx", "funding")["data"][0]["fundingRate"])
    await ex.close()


async def test_fetch_funding_rate_spot_not_supported() -> None:
    ex = OKX()
    with pytest.raises(NotSupportedError):
        await ex.fetch_funding_rate("BTC/USDT")
    await ex.close()


# ── positions ──


async def test_fetch_positions_spot_not_supported() -> None:
    ex = OKX(api_key="k", secret="s", passphrase="p")
    with pytest.raises(NotSupportedError):
        await ex.fetch_positions()
    await ex.close()


def test_parse_position_net_mode_sign_and_zero_liqpx_is_none() -> None:
    long_pos = _parse_position(
        "BTC/USDT:USDT",
        {"posSide": "net", "pos": "2", "avgPx": "70000.0", "upl": "100.5", "lever": "10", "liqPx": "60000.0"},
    )
    assert long_pos.side == "long"
    assert long_pos.amount == 2.0
    assert long_pos.liquidation_price == 60000.0

    short_pos = _parse_position(
        "ETH/USDT:USDT",
        {"posSide": "net", "pos": "-3", "avgPx": "3000.0", "upl": "-10.0", "lever": "5", "liqPx": "0"},
    )
    assert short_pos.side == "short"
    assert short_pos.amount == 3.0
    assert short_pos.liquidation_price is None  # "0" -> None, not 0.0

    explicit_short = _parse_position(
        "BTC/USDT:USDT",
        {"posSide": "short", "pos": "1.5", "avgPx": "70000.0", "upl": "5.0", "lever": "3", "liqPx": "90000.0"},
    )
    assert explicit_short.side == "short"


async def test_fetch_positions_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "net",
                    "pos": "2",
                    "avgPx": "70000.0",
                    "upl": "100.5",
                    "lever": "10",
                    "liqPx": "60000.0",
                    "uTime": "1700000000000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "net",
                    "pos": "-3",
                    "avgPx": "3000.0",
                    "upl": "-10.0",
                    "lever": "5",
                    "liqPx": "0",
                    "uTime": "1700000000000",
                },
                {
                    "instId": "SOL-USDT-SWAP",
                    "posSide": "net",
                    "pos": "0",
                    "avgPx": "0",
                    "upl": "0",
                    "lever": "20",
                    "liqPx": "0",
                    "uTime": "0",
                },
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p", market_type="linear")
    positions = await ex.fetch_positions()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/account/positions"
    assert req.url.params["instType"] == "SWAP"
    _assert_valid_signature(req)
    assert req.headers["OK-ACCESS-KEY"] == "k"
    assert req.headers["OK-ACCESS-PASSPHRASE"] == "p"
    assert len(positions) == 2  # flat SOL (pos=0) excluded
    btc = next(p for p in positions if p.symbol == "BTC/USDT:USDT")
    assert btc.side == "long"
    assert btc.amount == 2.0
    assert btc.entry_price == 70000.0
    assert btc.unrealized_pnl == 100.5
    assert btc.leverage == 10.0
    assert btc.liquidation_price == 60000.0
    eth = next(p for p in positions if p.symbol == "ETH/USDT:USDT")
    assert eth.side == "short"
    assert eth.amount == 3.0
    await ex.close()


async def test_x_simulated_trading_header_only_when_sandbox(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    sandboxed = OKX(api_key="k", secret="s", passphrase="p", market_type="linear", sandbox=True)
    await sandboxed.fetch_positions()
    sandboxed_req = httpx_mock.get_requests()[-1]
    assert sandboxed_req.headers["x-simulated-trading"] == "1"
    await sandboxed.close()

    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    live = OKX(api_key="k", secret="s", passphrase="p", market_type="linear", sandbox=False)
    await live.fetch_positions()
    live_req = httpx_mock.get_requests()[-1]
    assert "x-simulated-trading" not in live_req.headers
    await live.close()


# ── my trades (fills) ──


def test_parse_my_trade() -> None:
    t = _parse_my_trade(
        "BTC/USDT:USDT",
        {
            "instId": "BTC-USDT-SWAP",
            "tradeId": "12345",
            "ordId": "67890",
            "side": "buy",
            "fillPx": "70000.0",
            "fillSz": "0.01",
            "fee": "-0.007",
            "feeCcy": "USDT",
            "ts": "1700000000000",
        },
    )
    assert t.id == "12345"
    assert t.order_id == "67890"
    assert t.side == "buy"
    assert t.price == 70000.0
    assert t.amount == 0.01
    assert t.fee == -0.007
    assert t.fee_asset == "USDT"


async def test_fetch_my_trades_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "tradeId": "12345",
                    "ordId": "67890",
                    "side": "buy",
                    "fillPx": "70000.0",
                    "fillSz": "0.01",
                    "fee": "-0.007",
                    "feeCcy": "USDT",
                    "ts": "1700000000000",
                }
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p", market_type="linear")
    trades = await ex.fetch_my_trades("BTC/USDT:USDT", limit=50)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/trade/fills"
    assert req.url.params["instType"] == "SWAP"
    assert req.url.params["instId"] == "BTC-USDT-SWAP"
    assert req.url.params["limit"] == "50"
    _assert_valid_signature(req)
    assert req.headers["OK-ACCESS-KEY"] == "k"
    assert len(trades) == 1
    assert trades[0].symbol == "BTC/USDT:USDT"
    await ex.close()


async def test_fetch_my_trades_since_filters_client_side(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "BTC-USDT",
                    "tradeId": "1",
                    "ordId": "1",
                    "side": "buy",
                    "fillPx": "1",
                    "fillSz": "1",
                    "fee": "0",
                    "feeCcy": "USDT",
                    "ts": "1000",
                },
                {
                    "instId": "BTC-USDT",
                    "tradeId": "2",
                    "ordId": "2",
                    "side": "buy",
                    "fillPx": "1",
                    "fillSz": "1",
                    "fee": "0",
                    "feeCcy": "USDT",
                    "ts": "2000",
                },
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p")
    trades = await ex.fetch_my_trades("BTC/USDT", since=1500)
    assert [t.id for t in trades] == ["2"]
    await ex.close()


async def test_fetch_my_trades_no_symbol_resolves_via_from_native(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "ETH-USDT-SWAP",
                    "tradeId": "5",
                    "ordId": "9",
                    "side": "sell",
                    "fillPx": "3000",
                    "fillSz": "1",
                    "fee": "0",
                    "feeCcy": "USDT",
                    "ts": "1000",
                }
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p", market_type="linear")
    trades = await ex.fetch_my_trades()
    req = httpx_mock.get_request()
    assert "instId" not in req.url.params
    assert trades[0].symbol == "ETH/USDT:USDT"
    await ex.close()


# ── my trades history (fills-history, last 3 months) ──
# 대표 10-10 결정 — run /home/quantus/runs/global-mcp-okx-history-1010


async def test_fetch_my_trades_history_uses_three_month_endpoint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "BTC-USDT",
                    "instType": "SPOT",
                    "tradeId": "7",
                    "ordId": "42",
                    "billId": "900",
                    "side": "buy",
                    "fillPx": "100",
                    "fillSz": "1",
                    "fee": "-0.01",
                    "feeCcy": "BTC",
                    "fillTime": "1000",
                    "ts": "1000",
                }
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p")
    trades = await ex.fetch_my_trades_history("BTC/USDT", order_id="42", after="901", limit=100)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/trade/fills-history"
    assert dict(req.url.params) == {
        "instType": "SPOT",
        "instId": "BTC-USDT",
        "limit": "100",
        "ordId": "42",
        "after": "901",
    }
    _assert_valid_signature(req)
    assert [(t.id, t.order_id, t.fee, t.fee_asset) for t in trades] == [("7", "42", -0.01, "BTC")]
    assert trades[0].raw["billId"] == "900"
    await ex.close()


async def test_fetch_my_trades_history_rejects_bad_cursors_without_request(httpx_mock: HTTPXMock) -> None:
    ex = OKX(api_key="k", secret="s", passphrase="p")
    for kwargs in ({"after": "1", "before": "2"}, {"order_id": "4x"}, {"after": 5}):
        with pytest.raises(InvalidOrderError):
            await ex.fetch_my_trades_history("BTC/USDT", **kwargs)
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_fetch_my_trades_default_stays_three_day_endpoint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    assert await ex.fetch_my_trades("BTC/USDT", order_id="42") == []
    assert httpx_mock.get_request().url.path == "/api/v5/trade/fills"
    await ex.close()


# ── create_order: tdMode by market type, no posSide (one-way mode) ──


async def test_create_order_linear_uses_td_mode_and_no_pos_side(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"code": "0", "msg": "", "data": [{"ordId": "1"}]},
    )
    ex = OKX(api_key="k", secret="s", passphrase="p", market_type="linear", td_mode="isolated")
    order = await ex.create_order("BTC/USDT:USDT", "buy", "market", 1)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v5/trade/order"
    body = json_lib.loads(req.content.decode())
    assert body["instId"] == "BTC-USDT-SWAP"
    assert body["tdMode"] == "isolated"
    assert "posSide" not in body
    assert order.amount == 1
    assert order.side == "buy"
    assert order.type == "market"
    await ex.close()


async def test_create_order_spot_td_mode_comes_from_account_level(httpx_mock: HTTPXMock) -> None:
    """현물 tdMode 는 고정값이 아니라 실측한 acctLv 에서 나온다 — 자세한 것은
    tests/exchanges/test_okx_td_mode.py (A-3)."""
    httpx_mock.add_response(json=_ACCT_CONFIG)
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": [{"ordId": "1"}]})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    body = json_lib.loads(httpx_mock.get_requests()[-1].content.decode())
    assert body["tdMode"] == "cash"
    await ex.close()


# ── create_order/cancel_order: signature covers the verbatim wire body ──


async def test_create_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    """The prehash must be computed over the exact bytes sent on the wire, not
    over a dict that gets re-serialized separately by httpx — otherwise every
    real order would fail OKX's signature check. Recomputes independently from
    the real captured request rather than calling the adapter's own signer."""
    httpx_mock.add_response(json=_ACCT_CONFIG)
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": [{"ordId": "1"}]})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    req = httpx_mock.get_requests()[-1]
    assert req.headers["Content-Type"] == "application/json"
    _assert_valid_signature(req)
    await ex.close()


async def test_cancel_order_signature_matches_verbatim_wire_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": [{"ordId": "1"}]})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    await ex.cancel_order("1", "BTC/USDT")
    req = httpx_mock.get_request()
    assert req.headers["Content-Type"] == "application/json"
    _assert_valid_signature(req)
    await ex.close()


# ── create_order/cancel_order: sCode rejection (HTTP 200, code=="0") ──


async def test_create_order_scode_rejection_raises_insufficient_balance(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [{"ordId": "", "clOrdId": "", "tag": "", "sCode": "51008", "sMsg": "Insufficient balance"}],
        }
    )
    ex = _spot_with_known_account_level()
    with pytest.raises(InsufficientBalanceError):
        await ex.create_order("BTC/USDT", "buy", "market", 1000)
    await ex.close()


async def test_cancel_order_scode_rejection_raises_order_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [{"ordId": "1", "clOrdId": "", "sCode": "51603", "sMsg": "Order does not exist"}],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p")
    with pytest.raises(OrderNotFoundError):
        await ex.cancel_order("1", "BTC/USDT")
    await ex.close()


async def test_create_order_scode_zero_does_not_raise(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={"code": "0", "msg": "", "data": [{"ordId": "1", "clOrdId": "", "sCode": "0", "sMsg": ""}]}
    )
    ex = _spot_with_known_account_level()
    order = await ex.create_order("BTC/USDT", "buy", "market", 1)
    assert order.id == "1"
    await ex.close()


# ── error mapping ──


async def test_map_error_insufficient_balance(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "51008", "msg": "Order failed. Insufficient balance", "data": []})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    with pytest.raises(InsufficientBalanceError):
        await ex.fetch_balance()
    await ex.close()


def test_map_error_authentication() -> None:
    err = _map_error("50111", "Invalid OK-ACCESS-KEY")
    assert isinstance(err, AuthenticationError)


def test_map_error_order_not_found() -> None:
    err = _map_error("51603", "Order does not exist")
    assert isinstance(err, OrderNotFoundError)


def test_map_error_rate_limit() -> None:
    err = _map_error("50011", "Too Many Requests")
    assert isinstance(err, RateLimitError)


async def test_map_error_on_http_400_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"code": "50114", "msg": "Invalid Authorization"})
    ex = OKX(api_key="k", secret="s", passphrase="p")
    with pytest.raises(AuthenticationError):
        await ex.fetch_balance()
    await ex.close()


@pytest.mark.parametrize("method,path", [("fetch_ticker", "ticker"), ("fetch_order_book", "books")])
async def test_empty_data_raises_exchange_error(httpx_mock: HTTPXMock, method: str, path: str) -> None:
    """OKX answers an unknown//delisted instId with code 0 and an empty `data`
    array; indexing it blew up with IndexError (order book quietly returned an
    empty book) instead of a PyCexError."""
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": []})
    ex = OKX()
    with pytest.raises(ExchangeError):
        await getattr(ex, method)("BTC/USDT")
    await ex.close()

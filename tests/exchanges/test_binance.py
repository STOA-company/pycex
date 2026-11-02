"""Tests for the Binance adapter: markets, my-trades, and the USDT-M linear surface.

Fixtures under tests/fixtures/binance/ are real recorded responses (Task 0):
markets_spot (GET /api/v3/exchangeInfo, BTCUSDT), markets_linear (GET
/fapi/v1/exchangeInfo, BTCUSDT/ETHUSDT/SOLUSDT), candles_linear_1d (GET
/fapi/v1/klines, index 0 = open time ms — same 12-column shape as spot), and
funding (GET /fapi/v1/premiumIndex, BTCUSDT). Endpoint shapes confirmed against
developers.binance.com/docs/derivatives/usds-margined-futures/{market-data,trade}/rest-api
and developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints
(2026-08-30).
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl

import pytest
from pytest_httpx import HTTPXMock

from pycex.constants import BINANCE_FAPI, BINANCE_FAPI_TESTNET, BINANCE_FAPI_TESTNET_LEGACY
from pycex.exceptions import NotSupportedError
from pycex.exchanges.binance import Binance, _parse_balance_linear, _parse_funding, _parse_market, _parse_trade
from tests.conftest import load_fixture

SECRET = "s"


def _assert_valid_signature(query: str, secret: str = SECRET) -> None:
    """Recompute HMAC-SHA256 independently (plain ``hmac``, not the adapter's own
    ``binance_sign``) over the real outgoing query string minus ``signature``, and
    assert it matches — a self-referential call to ``binance_sign`` would be a
    tautology."""
    pairs = parse_qsl(query, keep_blank_values=True)
    signature = dict(pairs).get("signature")
    assert signature is not None
    message = "&".join(f"{k}={v}" for k, v in pairs if k != "signature")
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


# ── Construction ──


def test_linear_base_url() -> None:
    ex = Binance(api_key="k", secret="s", market_type="linear")
    assert ex._http._base_url == BINANCE_FAPI
    assert ex.market_type == "linear"


def test_linear_sandbox_base_url() -> None:
    ex = Binance(api_key="k", secret="s", market_type="linear", sandbox=True)
    assert ex._http._base_url == BINANCE_FAPI_TESTNET
    # Hosts per Binance docs (checked 2026-11-01): futures demo-fapi, spot testnet.binance.vision.
    assert BINANCE_FAPI_TESTNET == "https://demo-fapi.binance.com"
    assert BINANCE_FAPI_TESTNET_LEGACY == "https://testnet.binancefuture.com"
    assert BINANCE_FAPI_TESTNET not in (BINANCE_FAPI, BINANCE_FAPI_TESTNET_LEGACY)
    spot = Binance(api_key="k", secret="s", sandbox=True)
    assert spot._http._base_url == "https://testnet.binance.vision"
    live = Binance(api_key="k", secret="s", market_type="linear")
    assert live._http._base_url == "https://fapi.binance.com"


def test_to_native_linear_symbol() -> None:
    ex = Binance(api_key="k", secret="s", market_type="linear")
    assert ex.to_native("BTC/USDT:USDT") == "BTCUSDT"


def test_from_native_linear_fallback_no_cache() -> None:
    ex = Binance(api_key="k", secret="s", market_type="linear")
    assert ex.from_native("BTCUSDT") == "BTC/USDT:USDT"


def test_from_native_spot_fallback_no_cache() -> None:
    ex = Binance(api_key="k", secret="s")
    assert ex.from_native("BTCUSDT") == "BTC/USDT"


# ── fetch_markets parsing ──


def test_parse_market_spot_ticks_floats() -> None:
    raw = load_fixture("binance", "markets_spot")["symbols"][0]
    m = _parse_market(raw, "spot")
    assert m.symbol == "BTC/USDT"
    assert m.native == "BTCUSDT"
    assert m.base == "BTC" and m.quote == "USDT"
    assert m.price_tick == 0.01
    assert m.amount_step == 0.00001
    assert m.min_notional == 5.0
    assert m.active is True


def test_parse_market_linear_ticks_floats() -> None:
    raw = load_fixture("binance", "markets_linear")["symbols"][0]
    m = _parse_market(raw, "linear")
    assert m.symbol == "BTC/USDT:USDT"
    assert m.native == "BTCUSDT"
    assert m.market_type == "linear"
    assert m.price_tick == 0.10
    assert m.amount_step == 0.001
    assert m.min_notional == 50.0
    assert m.active is True


async def test_fetch_markets_spot_populates_cache(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("binance", "markets_spot"))
    ex = Binance()
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/exchangeInfo"
    assert len(markets) == 1
    assert ex.from_native("BTCUSDT") == "BTC/USDT"
    await ex.close()


async def test_fetch_markets_linear_populates_cache_and_symbol(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("binance", "markets_linear"))
    ex = Binance(market_type="linear")
    markets = await ex.fetch_markets()
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v1/exchangeInfo"
    assert {m.symbol for m in markets} == {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"}
    assert ex.from_native("ETHUSDT") == "ETH/USDT:USDT"
    await ex.close()


# ── funding rate ──


def test_parse_funding() -> None:
    raw = load_fixture("binance", "funding")
    f = _parse_funding("BTC/USDT:USDT", raw)
    assert f.rate == float(raw["lastFundingRate"])
    assert f.next_funding_time == raw["nextFundingTime"]
    assert f.interval_hours == 8


async def test_fetch_funding_rate_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("binance", "funding"))
    ex = Binance(market_type="linear")
    fr = await ex.fetch_funding_rate("BTC/USDT:USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v1/premiumIndex"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert fr.symbol == "BTC/USDT:USDT"
    assert fr.rate == float(load_fixture("binance", "funding")["lastFundingRate"])
    await ex.close()


async def test_fetch_funding_rate_spot_not_supported() -> None:
    ex = Binance()
    with pytest.raises(NotSupportedError):
        await ex.fetch_funding_rate("BTC/USDT")
    await ex.close()


# ── positions ──


async def test_fetch_positions_spot_not_supported() -> None:
    ex = Binance(api_key="k", secret="s")
    with pytest.raises(NotSupportedError):
        await ex.fetch_positions()
    await ex.close()


async def test_fetch_positions_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.500",
                "entryPrice": "70000.0",
                "unRealizedProfit": "100.5",
                "leverage": "10",
                "liquidationPrice": "60000.0",
                "updateTime": 1700000000000,
            },
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-2.000",
                "entryPrice": "3000.0",
                "unRealizedProfit": "-10.0",
                "leverage": "5",
                "liquidationPrice": "3500.0",
                "updateTime": 1700000000000,
            },
            {
                "symbol": "SOLUSDT",
                "positionAmt": "0",
                "entryPrice": "0.0",
                "unRealizedProfit": "0",
                "leverage": "20",
                "liquidationPrice": "0",
                "updateTime": 0,
            },
        ]
    )
    ex = Binance(api_key="k", secret="s", market_type="linear")
    positions = await ex.fetch_positions()
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v2/positionRisk"
    _assert_valid_signature(req.url.query.decode())
    assert req.headers["X-MBX-APIKEY"] == "k"
    assert len(positions) == 2  # flat SOLUSDT (positionAmt=0) excluded
    btc = next(p for p in positions if p.symbol == "BTC/USDT:USDT")
    assert btc.side == "long"
    assert btc.amount == 0.5
    assert btc.entry_price == 70000.0
    assert btc.unrealized_pnl == 100.5
    assert btc.leverage == 10.0
    assert btc.liquidation_price == 60000.0
    eth = next(p for p in positions if p.symbol == "ETH/USDT:USDT")
    assert eth.side == "short"
    assert eth.amount == 2.0
    await ex.close()


async def test_fetch_positions_linear_zero_liquidation_price_is_none(httpx_mock: HTTPXMock) -> None:
    """A cross-margin position can carry `liquidationPrice: "0"` (Binance can't
    compute one) while still being a real, non-flat position — `"0"` must map to
    `None`, not `0.0`, same treatment as `_parse_order`'s `price` field."""
    httpx_mock.add_response(
        json=[
            {
                "symbol": "SOLUSDT",
                "positionAmt": "10.000",
                "entryPrice": "150.0",
                "unRealizedProfit": "5.0",
                "leverage": "3",
                "liquidationPrice": "0",
                "updateTime": 1700000000000,
            }
        ]
    )
    ex = Binance(api_key="k", secret="s", market_type="linear")
    positions = await ex.fetch_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.amount != 0
    assert p.liquidation_price is None
    await ex.close()


# ── my trades ──


async def test_fetch_my_trades_requires_symbol() -> None:
    ex = Binance(api_key="k", secret="s")
    with pytest.raises(ValueError, match="requires symbol"):
        await ex.fetch_my_trades()
    await ex.close()


async def test_fetch_my_trades_spot(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[
            {
                "id": 28457,
                "orderId": 100234,
                "price": "4.00000100",
                "qty": "12.00000000",
                "commission": "10.10000000",
                "commissionAsset": "BNB",
                "time": 1499865549590,
                "isBuyer": True,
            }
        ]
    )
    ex = Binance(api_key="k", secret="s")
    trades = await ex.fetch_my_trades("BTC/USDT")
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/myTrades"
    assert req.url.params["symbol"] == "BTCUSDT"
    _assert_valid_signature(req.url.query.decode())
    assert req.headers["X-MBX-APIKEY"] == "k"
    assert len(trades) == 1
    t = trades[0]
    assert t.id == "28457"
    assert t.order_id == "100234"
    assert t.side == "buy"
    assert t.price == 4.000001
    assert t.amount == 12.0
    assert t.fee == 10.1
    assert t.fee_asset == "BNB"
    await ex.close()


async def test_fetch_my_trades_linear_isbuyer_absent_uses_side(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[
            {
                "id": 698759,
                "orderId": 25851813,
                "price": "7819.01",
                "qty": "0.002",
                "commission": "0.07819010",
                "commissionAsset": "USDT",
                "time": 1569514978020,
                "side": "SELL",
            }
        ]
    )
    ex = Binance(api_key="k", secret="s", market_type="linear")
    trades = await ex.fetch_my_trades("BTC/USDT:USDT", since=1569514978020, limit=50)
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v1/userTrades"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["startTime"] == "1569514978020"
    assert req.url.params["limit"] == "50"
    _assert_valid_signature(req.url.query.decode())
    assert trades[0].side == "sell"
    await ex.close()


# ── candles routing ──


async def test_fetch_candles_page_linear_hits_fapi(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("binance", "candles_linear_1d"))
    ex = Binance(market_type="linear")
    candles = await ex._fetch_candles_page("BTCUSDT", "1d", since=1787875200000, until=None, limit=3)
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v1/klines"
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["interval"] == "1d"
    assert req.url.params["startTime"] == "1787875200000"
    assert len(candles) == 3
    assert candles[0].timestamp == 1787875200000
    await ex.close()


async def test_fetch_candles_page_spot_hits_api_v3(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[])
    ex = Binance()
    await ex._fetch_candles_page("BTCUSDT", "1d", since=None, until=None, limit=3)
    req = httpx_mock.get_request()
    assert req.url.path == "/api/v3/klines"
    await ex.close()


# ── balance ──


async def test_fetch_balance_linear(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[
            {"asset": "USDT", "balance": "1000.0", "availableBalance": "800.0"},
            {"asset": "BNB", "balance": "0", "availableBalance": "0"},
        ]
    )
    ex = Binance(api_key="k", secret="s", market_type="linear")
    balance = await ex.fetch_balance()
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v2/balance"
    _assert_valid_signature(req.url.query.decode())
    usdt = balance.get("USDT")
    assert usdt is not None
    assert usdt.free == 800.0
    assert usdt.locked == 200.0
    assert balance.get("BNB") is None  # zero balance excluded
    await ex.close()


# ── order creation: no positionSide (one-way mode) ──


async def test_create_order_linear_no_position_side(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "orderId": 1,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "0.01",
            "executedQty": "0.01",
            "status": "FILLED",
            "updateTime": 1700000000000,
        }
    )
    ex = Binance(api_key="k", secret="s", market_type="linear")
    order = await ex.create_order("BTC/USDT:USDT", "buy", "market", 0.01)
    req = httpx_mock.get_request()
    assert req.url.path == "/fapi/v1/order"
    assert "positionSide" not in req.url.params
    _assert_valid_signature(req.url.query.decode())
    assert order.amount == 0.01
    assert order.side == "buy"
    assert order.type == "market"
    await ex.close()


@pytest.mark.parametrize("side,pos", [("buy", "LONG"), ("sell", "LONG"), ("sell", "SHORT"), ("buy", "SHORT")])
async def test_create_order_linear_hedge_position_side(httpx_mock: HTTPXMock, side: str, pos: str) -> None:
    httpx_mock.add_response(json={"orderId": 1, "symbol": "BTCUSDT", "side": side.upper(), "type": "MARKET",
                                  "origQty": "0.01", "executedQty": "0", "status": "NEW", "updateTime": 1})
    ex = Binance(api_key="k", secret="s", market_type="linear")
    await ex.create_order("BTC/USDT:USDT", side, "market", 0.01, position_side=pos)
    req = httpx_mock.get_request()
    assert req.url.params["positionSide"] == pos
    assert "reduceOnly" not in req.url.params
    await ex.close()


async def test_create_order_linear_hedge_refuses_reduce_only_and_bad_side() -> None:
    from pycex.exceptions import InvalidOrderError

    ex = Binance(api_key="k", secret="s", market_type="linear")
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT:USDT", "sell", "market", 0.01, reduce_only=True, position_side="LONG")
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT:USDT", "sell", "market", 0.01, position_side="BOTH")
    await ex.close()


def test_parse_public_trade_side_is_the_taker_side() -> None:
    """``isBuyerMaker=true`` means the BUYER was the maker, so the taker SOLD.

    Every other adapter reports the taker side (Korbit's ``isBuyerTaker``, OKX/
    Bitget/Bybit's explicit ``side``), and so does ccxt for this endpoint; this
    parser had the mapping inverted.
    """
    maker_buy = _parse_trade("BTC/USDT", {"id": 1, "price": "1", "qty": "2", "time": 3, "isBuyerMaker": True})
    taker_buy = _parse_trade("BTC/USDT", {"id": 2, "price": "1", "qty": "2", "time": 3, "isBuyerMaker": False})
    assert maker_buy.side == "sell"
    assert taker_buy.side == "buy"


def test_parse_balance_linear_keeps_the_actual_response_as_raw() -> None:
    """GET /fapi/v2/balance returns a bare list; `raw` used to wrap it in a
    `{"balances": [...]}` envelope that the endpoint never sent — the exact shape
    spot returns, so `raw` lied about which endpoint answered."""
    data = [{"asset": "USDT", "balance": "10", "availableBalance": "7"}]
    assert _parse_balance_linear(data).raw == data

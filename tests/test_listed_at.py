"""F2 — Market.listed_at is the venue listing time, or None when the venue omits it.

Expected instants are literals. A parser that reads the wrong field, or a
fixture whose listing timestamp changes, fails.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pycex.exchanges.binance import _parse_market as parse_binance
from pycex.exchanges.bitget import _parse_market as parse_bitget
from pycex.exchanges.bithumb import _parse_market as parse_bithumb
from pycex.exchanges.bybit import _parse_market as parse_bybit
from pycex.exchanges.korbit import _parse_market as parse_korbit
from pycex.exchanges.okx import _parse_market as parse_okx
from pycex.exchanges.upbit import _parse_market as parse_upbit
from pycex.models import Market
from tests.conftest import load_fixture

_UTC = timezone.utc


def test_market_listed_at_defaults_to_none() -> None:
    m = Market(symbol="BTC/KRW", native="KRW-BTC", base="BTC", quote="KRW", market_type="spot")
    assert m.listed_at is None


def test_okx_spot_listed_at_is_list_time() -> None:
    raw = load_fixture("okx", "markets_spot")["data"][0]
    assert raw["listTime"] == "1611907686000"
    listed = parse_okx(raw, "spot").listed_at
    assert listed == datetime(2021, 1, 29, 8, 8, 6, tzinfo=_UTC)


def test_okx_swap_listed_at_is_list_time() -> None:
    raw = load_fixture("okx", "markets_swap")["data"][0]
    assert raw["listTime"] == "1573557408000"
    listed = parse_okx(raw, "linear").listed_at
    assert listed == datetime(2019, 11, 12, 11, 16, 48, tzinfo=_UTC)


def test_binance_linear_listed_at_is_onboard_date() -> None:
    raw = load_fixture("binance", "markets_linear")["symbols"][0]
    assert raw["onboardDate"] == 1567965300000
    listed = parse_binance(raw, "linear").listed_at
    assert listed == datetime(2019, 9, 8, 17, 55, tzinfo=_UTC)


def test_binance_spot_listed_at_is_none() -> None:
    raw = load_fixture("binance", "markets_spot")["symbols"][0]
    assert "onboardDate" not in raw
    assert parse_binance(raw, "spot").listed_at is None


def test_bitget_linear_blank_launch_time_is_none() -> None:
    raw = load_fixture("bitget", "markets_linear")["data"][0]
    assert raw["launchTime"] == ""
    assert parse_bitget(raw, "linear").listed_at is None


def test_bitget_linear_launch_time_beats_online_time() -> None:
    raw = dict(load_fixture("bitget", "markets_linear")["data"][0])
    raw["launchTime"] = "1600000000000"
    raw["onlineTime"] = "1500000000000"
    listed = parse_bitget(raw, "linear").listed_at
    assert listed == datetime(2020, 9, 13, 12, 26, 40, tzinfo=_UTC)


def test_bitget_linear_online_time_when_launch_time_blank() -> None:
    raw = dict(load_fixture("bitget", "markets_linear")["data"][0])
    raw["launchTime"] = ""
    raw["onlineTime"] = "1500000000000"
    listed = parse_bitget(raw, "linear").listed_at
    assert listed == datetime(2017, 7, 14, 2, 40, tzinfo=_UTC)


def test_bitget_spot_open_time_is_not_listed_at() -> None:
    """Spot publishes openTime, not launchTime/onlineTime. openTime is not the listing contract."""
    raw = load_fixture("bitget", "markets_spot")["data"][0]
    assert raw["openTime"] == "1532454360000"
    assert "launchTime" not in raw
    assert parse_bitget(raw, "spot").listed_at is None


def test_bybit_launch_time_when_present() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "status": "Trading",
        "launchTime": "1580000000000",
        "priceFilter": {"tickSize": "0.01"},
        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderAmt": "1"},
    }
    listed = parse_bybit(raw, "spot")
    assert listed is not None
    assert listed.listed_at == datetime(2020, 1, 26, 0, 53, 20, tzinfo=_UTC)


def test_bybit_listed_at_none_when_launch_time_absent() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.01"},
        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderAmt": "1"},
    }
    listed = parse_bybit(raw, "spot")
    assert listed is not None
    assert listed.listed_at is None


def test_upbit_bithumb_korbit_listed_at_is_none() -> None:
    assert parse_upbit(load_fixture("upbit", "markets")[0]).listed_at is None
    assert parse_bithumb(load_fixture("bithumb", "markets")[0]).listed_at is None
    assert parse_korbit(load_fixture("korbit", "markets")["data"][0]).listed_at is None

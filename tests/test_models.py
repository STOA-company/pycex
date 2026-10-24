"""Tests for unified Pydantic models."""

from pycex.exceptions import NotSupportedError, PyCexError
from pycex.models import (
    Balance,
    BalanceEntry,
    Candle,
    FundingRate,
    Market,
    MyTrade,
    Order,
    OrderBook,
    OrderBookEntry,
    Position,
    Ticker,
    Trade,
)


class TestTicker:
    def test_create(self) -> None:
        t = Ticker(symbol="BTCUSDT", last=50000.0, bid=49999.0, ask=50001.0, high=51000.0, low=49000.0, volume=1234.5)
        assert t.symbol == "BTCUSDT"
        assert t.last == 50000.0
        assert t.bid == 49999.0

    def test_raw(self) -> None:
        t = Ticker(symbol="BTCUSDT", last=50000.0, bid=0, ask=0, high=0, low=0, volume=0, raw={"custom": "data"})
        assert t.raw["custom"] == "data"


class TestOrderBook:
    def test_create(self) -> None:
        ob = OrderBook(
            symbol="BTCUSDT",
            bids=[OrderBookEntry(price=49999.0, amount=1.5)],
            asks=[OrderBookEntry(price=50001.0, amount=2.0)],
        )
        assert ob.symbol == "BTCUSDT"
        assert len(ob.bids) == 1
        assert ob.bids[0].price == 49999.0
        assert ob.asks[0].amount == 2.0


class TestCandle:
    def test_create(self) -> None:
        c = Candle(timestamp=1700000000, open=50000.0, high=51000.0, low=49000.0, close=50500.0, volume=100.0)
        assert c.open == 50000.0
        assert c.close == 50500.0


class TestBalanceEntry:
    def test_total(self) -> None:
        entry = BalanceEntry(asset="BTC", free=1.0, locked=0.5)
        assert entry.total == 1.5


class TestBalance:
    def test_get(self) -> None:
        bal = Balance(
            assets=[
                BalanceEntry(asset="BTC", free=1.0, locked=0.0),
                BalanceEntry(asset="ETH", free=10.0, locked=2.0),
            ]
        )
        btc = bal.get("btc")
        assert btc is not None
        assert btc.free == 1.0

    def test_get_missing(self) -> None:
        bal = Balance(assets=[])
        assert bal.get("XRP") is None


class TestOrder:
    def test_create(self) -> None:
        o = Order(id="123", symbol="BTCUSDT", side="buy", type="limit", amount=0.1, price=50000.0)
        assert o.id == "123"
        assert o.price == 50000.0
        assert o.filled == 0.0
        assert o.average_price is None

    def test_execution_average_is_separate_from_limit_price(self) -> None:
        o = Order(id="123", symbol="BTC/KRW", side="buy", type="limit", amount=2, price=5000, average_price=4900)
        assert o.price == 5000 and o.average_price == 4900
        assert Order.model_validate(o.model_dump()).average_price == 4900


class TestTrade:
    def test_create(self) -> None:
        t = Trade(id="456", symbol="ETHUSDT", side="sell", price=3000.0, amount=5.0, timestamp=1700000000)
        assert t.side == "sell"
        assert t.amount == 5.0


def test_market_defaults() -> None:
    m = Market(symbol="BTC/KRW", native="KRW-BTC", base="BTC", quote="KRW", market_type="spot")
    assert m.active and m.price_tick is None


def test_mytrade_fee_default() -> None:
    t = MyTrade(id="1", order_id="o", symbol="BTC/USDT", side="buy", price=1.0, amount=2.0)
    assert t.fee == 0.0 and t.fee_asset == ""


def test_position_flat() -> None:
    p = Position(symbol="BTC/USDT:USDT", side="flat", amount=0.0)
    assert p.unrealized_pnl == 0.0


def test_funding_rate_interval_default() -> None:
    f = FundingRate(symbol="BTC/USDT:USDT", rate=0.0001)
    assert f.interval_hours == 8


def test_not_supported_is_pycex_error() -> None:
    assert issubclass(NotSupportedError, PyCexError)


def test_every_model_is_exported_from_the_package_root() -> None:
    """`from pycex import Candle` was an ImportError: only 4 of the 12 models were
    re-exported, so callers had to reach into `pycex.models.*` for the rest."""
    import pycex
    import pycex.models

    for name in pycex.models.__all__:
        assert name in pycex.__all__, f"{name} missing from pycex.__all__"
        assert getattr(pycex, name) is getattr(pycex.models, name)

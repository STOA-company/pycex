import pytest

from pycex.exceptions import SymbolNotFoundError
from pycex.symbols import Symbol, linear, parse_symbol, spot


def test_parse_spot() -> None:
    s = parse_symbol("BTC/KRW")
    assert s == Symbol("BTC", "KRW", None)
    assert s.market_type == "spot"
    assert str(s) == "BTC/KRW"


def test_parse_linear() -> None:
    s = parse_symbol("BTC/USDT:USDT")
    assert s.settle == "USDT"
    assert s.market_type == "linear"
    assert str(s) == "BTC/USDT:USDT"


def test_case_normalised() -> None:
    assert str(parse_symbol("btc/usdt")) == "BTC/USDT"


def test_parse_unicode_base() -> None:
    s = parse_symbol("哈基米/USDT:USDT")
    assert s.base == "哈基米"
    assert s.quote == "USDT"
    assert s.settle == "USDT"
    assert str(s) == "哈基米/USDT:USDT"


@pytest.mark.parametrize("bad", ["BTCUSDT", "KRW-BTC", "BTC/", "/USDT", "BTC/USDT:", "BTC/USDT:USDT:X", ""])
def test_parse_rejects_native(bad: str) -> None:
    with pytest.raises(SymbolNotFoundError):
        parse_symbol(bad)


def test_builders() -> None:
    assert spot("eth", "krw") == "ETH/KRW"
    assert linear("ETH", "USDT") == "ETH/USDT:USDT"
    assert linear("ETH", "USD", "BTC") == "ETH/USD:BTC"

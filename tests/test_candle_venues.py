"""F4 — one code table is the candle page limit, paging direction, and timeframes.

The literals below are the contract. A venue constant, an adapter attribute,
or the README row that drifts from them fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pycex import constants
from pycex.exceptions import NotSupportedError
from pycex.exchanges import OKX, Binance, Bitget, Bithumb, Bybit, Korbit, Upbit

ROOT = Path(__file__).resolve().parents[1]
_TF = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})

# exchange key, class, page limit, paging, README row prefix
_ROWS = (
    ("binance", Binance, 200, "forward", "| Binance (spot·linear) | 200 | forward | open |"),
    ("bybit", Bybit, 200, "backward", "| Bybit (spot·linear) | 200 | backward | open |"),
    ("okx", OKX, 100, "backward", "| OKX (spot·linear) | 100 | backward | open |"),
    ("bitget", Bitget, 200, "backward", "| Bitget (spot·linear) | 200 | backward | open |"),
    ("upbit", Upbit, 200, "backward", "| Upbit | 200 | backward | open |"),
    ("bithumb", Bithumb, 200, "backward", "| Bithumb | 200 | backward | open |"),
    ("korbit", Korbit, 200, "backward", "| Korbit | 200 | backward | open |"),
)


def test_candle_venue_table_is_the_adapter_and_the_readme() -> None:
    table = getattr(constants, "CANDLE_VENUES", None)
    assert table is not None
    assert set(table) == {row[0] for row in _ROWS}
    readme = (ROOT / "README.md").read_text()
    for key, cls, limit, paging, row in _ROWS:
        spec = table[key]
        assert spec.page_limit == limit
        assert spec.paging == paging
        assert spec.bar_time == "open"
        assert spec.timeframes == _TF
        assert cls.candle_page_limit == limit
        assert cls.candle_paging == paging
        assert cls.supported_timeframes is spec.timeframes
        assert row in readme


@pytest.mark.parametrize("cls", [Binance, Bybit, OKX, Bitget, Upbit, Bithumb, Korbit])
@pytest.mark.parametrize("timeframe", ["3m", "1w"])
async def test_unsupported_timeframe_raises(cls: type, timeframe: str) -> None:
    ex = cls()
    try:
        with pytest.raises(NotSupportedError):
            await ex.fetch_candles("BTC/USDT", timeframe)
    finally:
        await ex.close()

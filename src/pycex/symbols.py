"""Canonical symbol notation: spot ``BASE/QUOTE``, linear perpetual ``BASE/QUOTE:SETTLE``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pycex.exceptions import SymbolNotFoundError

MarketType = Literal["spot", "linear"]

# Quote/settle stay ASCII. Base allows Unicode letters so venue-native CJK
# tickers (Binance ``哈基米/USDT:USDT``) parse; ``str.upper`` leaves them intact.
_RE = re.compile(r"^([\w]+)/([A-Z0-9]+)(?::([A-Z0-9]+))?$", re.UNICODE)


@dataclass(frozen=True)
class Symbol:
    base: str
    quote: str
    settle: str | None = None

    @property
    def market_type(self) -> MarketType:
        return "linear" if self.settle else "spot"

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}" + (f":{self.settle}" if self.settle else "")


def parse_symbol(s: str) -> Symbol:
    m = _RE.match(s.upper())
    if not m:
        raise SymbolNotFoundError(f"not a canonical symbol: {s!r} (expected BASE/QUOTE or BASE/QUOTE:SETTLE)")
    return Symbol(m.group(1), m.group(2), m.group(3))


def spot(base: str, quote: str) -> str:
    return f"{base.upper()}/{quote.upper()}"


def linear(base: str, quote: str, settle: str | None = None) -> str:
    return f"{base.upper()}/{quote.upper()}:{(settle or quote).upper()}"

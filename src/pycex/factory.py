"""Shared exchange-construction factory used by both the CLI (``pycex.cli``)
and the MCP server (``pycex.mcp.server``) — the single place that knows how
to turn an exchange name plus credentials into a configured adapter, so the
two entry points can never drift out of sync on which exchanges exist or how
credentials/env vars are resolved.
"""

from __future__ import annotations

import os

from pycex.base import BaseExchange
from pycex.exchanges import OKX, Binance, Bitget, Bithumb, Bybit, DigitalX, Korbit, Upbit
from pycex.symbols import MarketType

# Public registry: name -> concrete adapter class. Exposed for callers that
# want to validate a name or enumerate the supported exchanges (e.g. CLI
# --help text, tests) without constructing anything.
EXCHANGES: dict[str, type[BaseExchange]] = {
    "binance": Binance,
    "bybit": Bybit,
    "okx": OKX,
    "bitget": Bitget,
    "upbit": Upbit,
    "bithumb": Bithumb,
    "korbit": Korbit,
    "digitalx": DigitalX,
}


def create_exchange(
    name: str,
    *,
    api_key: str = "",
    secret: str = "",
    passphrase: str = "",
    sandbox: bool = False,
    market_type: MarketType = "spot",
) -> BaseExchange:
    """Construct one of the seven supported exchange adapters by name.

    Any credential left empty (``""``) is filled in from the environment as
    ``PYCEX_{NAME}_API_KEY`` / ``PYCEX_{NAME}_SECRET`` / ``PYCEX_{NAME}_PASSPHRASE``
    (``{NAME}`` upper-cased, e.g. ``PYCEX_BYBIT_API_KEY``). ``passphrase`` is
    only forwarded to OKX and Bitget — the only two adapters whose
    constructor accepts it; every other exchange silently ignores it.

    Raises ``ValueError`` for an unrecognized ``name``, listing the valid
    names in the message.
    """
    key = name.lower()
    if key not in EXCHANGES:
        raise ValueError(f"unknown exchange {name!r}; valid names: {', '.join(sorted(EXCHANGES))}")

    prefix = f"PYCEX_{key.upper()}_"
    api_key = api_key or os.environ.get(f"{prefix}API_KEY", "")
    secret = secret or os.environ.get(f"{prefix}SECRET", "")
    passphrase = passphrase or os.environ.get(f"{prefix}PASSPHRASE", "")

    if key == "binance":
        return Binance(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    if key == "bybit":
        return Bybit(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    if key == "okx":
        return OKX(api_key=api_key, secret=secret, passphrase=passphrase, sandbox=sandbox, market_type=market_type)
    if key == "bitget":
        return Bitget(api_key=api_key, secret=secret, passphrase=passphrase, sandbox=sandbox, market_type=market_type)
    if key == "upbit":
        return Upbit(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    if key == "bithumb":
        return Bithumb(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    if key == "digitalx":
        return DigitalX(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    if key == "korbit":
        return Korbit(api_key=api_key, secret=secret, sandbox=sandbox, market_type=market_type)
    raise AssertionError(f"unreachable: {key!r} is in EXCHANGES but has no constructor branch")

"""Tests for the shared exchange factory (``pycex.factory``) and its CLI
wiring (``pycex.cli._make_exchange``). The MCP server's ``_get_exchange``
delegates to the same factory and is exercised indirectly through these
factory-level tests (no separate MCP-specific test needed for the
construction path).
"""

from __future__ import annotations

import argparse
import inspect
import sys
import warnings
from typing import cast

import pytest

from pycex import cli
from pycex.base import BaseExchange
from pycex.cli import _make_exchange, main
from pycex.exchanges import OKX, Binance, Bitget, Bithumb, Bybit, DigitalX, Korbit, Upbit
from pycex.factory import EXCHANGES, create_exchange
from pycex.mcp import server as mcp_server


def _namespace(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "exchange": None,
        "api_key": None,
        "secret": None,
        "sandbox": False,
        "market_type": "spot",
        "testnet": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


# ── factory: eight exchanges ──

_EXPECTED_CLASSES: dict[str, type[BaseExchange]] = {
    "binance": Binance,
    "bybit": Bybit,
    "okx": OKX,
    "bitget": Bitget,
    "upbit": Upbit,
    "bithumb": Bithumb,
    "korbit": Korbit,
    "digitalx": DigitalX,
}

_PASSPHRASE_NAMES = ("okx", "bitget")


def _construct(name: str) -> BaseExchange:
    """Construct via the factory, supplying a passphrase only for the two
    exchanges whose constructor requires one."""
    if name.lower() in _PASSPHRASE_NAMES:
        return create_exchange(name, api_key="k", secret="s", passphrase="p")
    return create_exchange(name, api_key="k", secret="s")


def test_exchanges_registry_has_all_eight() -> None:
    assert set(EXCHANGES) == set(_EXPECTED_CLASSES)


@pytest.mark.parametrize("name,expected_cls", sorted(_EXPECTED_CLASSES.items()))
def test_create_exchange_returns_correct_class(name: str, expected_cls: type[BaseExchange]) -> None:
    ex = _construct(name)
    assert isinstance(ex, expected_cls)
    assert EXCHANGES[name] is expected_cls


@pytest.mark.parametrize("name", sorted(_EXPECTED_CLASSES))
def test_create_exchange_name_is_case_insensitive(name: str) -> None:
    ex = _construct(name.upper())
    assert isinstance(ex, _EXPECTED_CLASSES[name])


def test_create_exchange_passphrase_only_forwarded_to_okx_and_bitget() -> None:
    # Non-passphrase exchanges must not choke on the kwarg — the factory
    # simply never forwards it to a constructor that doesn't accept it.
    ex = create_exchange("binance", api_key="k", secret="s", passphrase="ignored")
    assert isinstance(ex, Binance)


def test_create_exchange_market_type_linear() -> None:
    ex = create_exchange("bybit", api_key="k", secret="s", market_type="linear")
    assert ex.market_type == "linear"


def test_create_exchange_sandbox_flag() -> None:
    ex = create_exchange("binance", api_key="k", secret="s", sandbox=True)
    assert ex.sandbox is True


def test_create_exchange_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown exchange"):
        create_exchange("not-a-real-exchange")


def test_create_exchange_unknown_error_lists_valid_names() -> None:
    with pytest.raises(ValueError) as e:
        create_exchange("nope")
    for name in EXCHANGES:
        assert name in str(e.value)


# ── factory: env var pickup ──


def test_create_exchange_reads_env_when_args_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYCEX_BYBIT_API_KEY", "env-key")
    monkeypatch.setenv("PYCEX_BYBIT_SECRET", "env-secret")
    ex = cast(Bybit, create_exchange("bybit"))
    assert ex._api_key == "env-key"
    assert ex._secret == "env-secret"


def test_create_exchange_reads_passphrase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYCEX_OKX_API_KEY", "env-key")
    monkeypatch.setenv("PYCEX_OKX_SECRET", "env-secret")
    monkeypatch.setenv("PYCEX_OKX_PASSPHRASE", "env-pass")
    ex = cast(OKX, create_exchange("okx"))
    assert ex._passphrase == "env-pass"


def test_create_exchange_explicit_args_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYCEX_BINANCE_API_KEY", "env-key")
    ex = cast(Binance, create_exchange("binance", api_key="explicit-key", secret="s"))
    assert ex._api_key == "explicit-key"


# ── CLI: _make_exchange ──


@pytest.mark.parametrize("name,expected_cls", sorted(_EXPECTED_CLASSES.items()))
def test_cli_make_exchange_returns_correct_class(name: str, expected_cls: type[BaseExchange]) -> None:
    args = _namespace(exchange=name, api_key="k", secret="s")
    ex = _make_exchange(args)
    assert isinstance(ex, expected_cls)


def test_cli_testnet_deprecated_maps_to_sandbox() -> None:
    args = _namespace(exchange="binance", api_key="k", secret="s", testnet=True, sandbox=False)
    with pytest.warns(DeprecationWarning):
        ex = _make_exchange(args)
    assert ex.sandbox is True


def test_cli_sandbox_flag_without_testnet_emits_no_warning() -> None:
    args = _namespace(exchange="binance", api_key="k", secret="s", sandbox=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ex = _make_exchange(args)
    assert ex.sandbox is True


def test_cli_market_type_linear() -> None:
    args = _namespace(exchange="bybit", api_key="k", secret="s", market_type="linear")
    ex = _make_exchange(args)
    assert ex.market_type == "linear"


def test_cli_unknown_exchange_exits_1() -> None:
    args = _namespace(exchange="not-a-real-exchange", api_key="k", secret="s")
    with pytest.raises(SystemExit) as e:
        _make_exchange(args)
    assert e.value.code == 1


def test_cli_adapter_constructor_rejection_exits_1_no_traceback() -> None:
    """Upbit's constructor raises NotSupportedError (a PyCexError, not a
    ValueError) for sandbox=True — this must exit cleanly with the message,
    the same as an unknown exchange name, not propagate as an unhandled
    exception."""
    args = _namespace(exchange="upbit", api_key="k", secret="s", sandbox=True)
    with pytest.raises(SystemExit) as e:
        _make_exchange(args)
    assert e.value.code == 1


def test_cli_default_exchange_is_binance_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYCEX_EXCHANGE", raising=False)
    args = _namespace(exchange=None, api_key="k", secret="s")
    ex = _make_exchange(args)
    assert isinstance(ex, Binance)


def test_symbol_help_advertises_canonical_notation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI takes canonical symbols (`to_native` converts) — advertising the
    exchange-native `BTCUSDT` handed users a string every adapter now rejects."""
    monkeypatch.setattr(sys, "argv", ["pycex", "ticker", "--help"])
    with pytest.raises(SystemExit):
        main()
    out = capsys.readouterr().out
    assert "BTC/USDT" in out
    assert "BTCUSDT" not in out


def test_user_facing_help_has_no_native_symbol_examples() -> None:
    for module in (cli, mcp_server):
        assert "BTCUSDT" not in inspect.getsource(module), f"{module.__name__} still advertises a native symbol"
    assert inspect.signature(mcp_server.market_analysis).parameters["symbol"].default == "BTC/USDT"

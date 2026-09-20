"""Exception hierarchy for pycex."""

from __future__ import annotations


class PyCexError(Exception):
    """Base exception for all pycex errors.

    ``retryable`` says only this: **the same request could succeed later**,
    because what blocked it is a condition of the moment (settlement lag, a
    rate limit) rather than a wrong request or a wrong account. pycex
    classifies; it never retries. How long to wait, how many times to look
    again, and when to give up is the caller's execution policy — a settlement
    -aware loop re-measures the available balance and only then re-sends. The
    default is ``False``: an error nobody has classified is not optimistically
    assumed to be temporary.
    """

    retryable: bool = False


class AuthenticationError(PyCexError):
    """Invalid or missing API credentials."""


class ExchangeError(PyCexError):
    """Error returned by the exchange API."""

    def __init__(self, message: str, *, code: str | int | None = None, exchange: str = "") -> None:
        self.code = code
        self.exchange = exchange
        super().__init__(message)


class RateLimitError(ExchangeError):
    """Request was rate-limited by the exchange."""

    retryable = True
    retry_after: float | None = None


class InsufficientBalanceError(ExchangeError):
    """Insufficient funds for the requested operation."""


class SettlementPendingError(InsufficientBalanceError):
    """The funds are not available **yet** — the previous fill has not settled.

    🚨 OKX answers this with ``51008`` ("Order failed. Your available BTC
    balance is insufficient..."). Measured live on 2026-09-10: a sell placed
    right after its buy filled was refused for ~70 seconds, then went through
    unchanged once ``availBal`` caught up (27 such rejections across the run).

    It stays a subclass of :class:`InsufficientBalanceError` — the funds really
    are not there right now — but it is marked ``retryable`` so a caller can
    tell "not yet" from "not ever". The answer to it is to re-measure the
    available balance (``fetch_available_balance``), not to fire the same order
    again blindly.
    """

    retryable = True


class InvalidOrderError(ExchangeError):
    """Order parameters are invalid."""


class OrderNotFoundError(ExchangeError):
    """Order was not found."""


class NetworkError(PyCexError):
    """Network-level error (timeout, connection refused, etc.)."""


class SymbolNotFoundError(PyCexError):
    """Trading pair/symbol does not exist on the exchange."""


class NotSupportedError(PyCexError):
    """The exchange or market type does not support this operation."""


class HedgeModeNotSupportedError(ExchangeError):
    """Hedge-mode accounts are not supported for order placement."""


class UnsupportedOrderError(NotSupportedError, InvalidOrderError):
    """Unsupported order option (also an InvalidOrderError for compatibility)."""

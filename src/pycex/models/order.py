"""Unified order model."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class Order(BaseModel):
    """Unified order data."""

    id: str
    #: Caller-supplied idempotency key echoed back by the venue (OKX ``clOrdId``).
    #: ``None`` when the caller did not supply one — pycex never invents one.
    client_order_id: str | None = None
    symbol: str
    side: str
    type: str
    # Exact venue values when supplied by a decimal-aware adapter.
    amount_exact: Decimal | None = None
    price_exact: Decimal | None = None
    filled_exact: Decimal | None = None
    amount: float
    price: float | None = None
    filled: float = 0.0
    status: str = ""
    timestamp: int = 0
    raw: dict[str, Any] = {}  # noqa: RUF012

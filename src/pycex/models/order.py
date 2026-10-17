"""Unified order model."""

from __future__ import annotations

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
    amount: float
    price: float | None = None
    filled: float = 0.0
    #: Actual execution average; never the caller's limit price.
    average_price: float | None = None
    status: str = ""
    timestamp: int = 0
    raw: dict[str, Any] = {}  # noqa: RUF012

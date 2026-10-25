"""Unified authenticated (own) trade model."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class MyTrade(BaseModel):
    """A fill on the caller's own account."""

    id: str
    order_id: str
    symbol: str
    side: str
    # Exact venue values when supplied by a decimal-aware adapter.
    amount_exact: Decimal | None = None
    price_exact: Decimal | None = None
    fee_exact: Decimal | None = None
    price: float
    amount: float
    fee: float = 0.0
    fee_asset: str = ""
    timestamp: int = 0
    raw: dict[str, Any] = {}  # noqa: RUF012

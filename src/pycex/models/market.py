"""Unified market metadata model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Market(BaseModel):
    """Trading pair/market metadata."""

    symbol: str  # canonical
    native: str  # exchange notation
    base: str
    quote: str
    market_type: str  # "spot" | "linear"
    price_tick: float | None = None
    amount_step: float | None = None
    min_notional: float | None = None
    active: bool = True
    raw: dict[str, Any] = {}  # noqa: RUF012
    public_rules: dict[str, Any] = {}  # Document-derived rules, units, scope and sources; not API raw.

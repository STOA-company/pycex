"""Unified market metadata model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


def parse_listing_time(value: Any) -> datetime | None:
    """Venue listing timestamp (epoch ms) as a UTC datetime.

    Blank, non-numeric, and non-positive values are ``None``. Callers choose
    which raw field is the listing time; this helper does not guess.
    """
    if value is None or value == "":
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


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
    listed_at: datetime | None = None
    raw: dict[str, Any] = {}  # noqa: RUF012
    public_rules: dict[str, Any] = {}  # Document-derived rules, units, scope and sources; not API raw.

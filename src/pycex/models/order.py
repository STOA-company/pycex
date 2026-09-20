"""Unified order model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Order(BaseModel):
    """Unified order data."""

    id: str
    #: Caller-supplied order ID echoed by the venue; not an exactly-once guarantee.
    #: ``None`` when the caller did not supply one — pycex never invents one.
    client_order_id: str | None = None
    symbol: str
    side: str
    type: str
    amount: float
    price: float | None = None
    average: float | None = None  # executed average price; never the limit price
    filled: float = 0.0
    status: str = ""
    timestamp: int = 0
    raw: dict[str, Any] = {}  # noqa: RUF012

    @property
    def normalized_status(self) -> str:
        """Common lifecycle; ``status`` retains the venue value for compatibility.

        ``accepted`` is only an acknowledgement, not evidence of a fill.
        Unknown/missing venue states remain ``unknown``.
        """
        return {
            "accepted": "accepted",
            "new": "open",
            "live": "open",
            "partially_filled": "partially_filled",
            "partial-fill": "partially_filled",
            "filled": "filled",
            "full-fill": "filled",
            "canceled": "canceled",
            "cancelled": "canceled",
            "mmp_canceled": "canceled",
            "expired": "expired",
            "expired_in_match": "expired",
            "rejected": "rejected",
        }.get(self.status.lower(), "unknown")

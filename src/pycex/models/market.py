"""Unified market metadata model."""

from __future__ import annotations

from decimal import Decimal, DecimalException, Inexact, InvalidOperation, localcontext
from typing import Any

from pydantic import BaseModel

from pycex.exceptions import InvalidOrderError, NotSupportedError


class Market(BaseModel):
    """Trading pair/market metadata."""

    symbol: str  # canonical
    native: str  # exchange notation
    base: str
    quote: str
    market_type: str  # "spot" | "linear"
    price_tick: float | None = None
    amount_step: float | None = None
    amount_unit: str | None = None  # "base" or "contract"; unit of order amount/filled
    contract_size: float | None = None  # base currency per contract (linear only)
    min_amount: float | None = None  # in amount_unit
    market_amount_step: float | None = None  # additional market-order filter, when supplied
    market_min_amount: float | None = None
    min_notional: float | None = None
    active: bool = True
    raw: dict[str, Any] = {}  # noqa: RUF012

    def amount_from_base(self, amount: float | str | Decimal) -> Decimal:
        """Convert base quantity to market-order units, exactly, without rounding.

        Reject below-minimum/off-step amounts instead of increasing an order.
        This does not validate quote notional or choose a sizing/retry policy.
        """
        if self.market_type != "linear" or self.amount_unit not in ("base", "contract"):
            raise NotSupportedError("Base conversion requires linear market quantity metadata")
        if self.min_amount is None or self.amount_step is None:
            raise NotSupportedError("Minimum amount and amount step are required")
        try:
            value = Decimal(str(amount))
            size = Decimal(str(self.contract_size)) if self.amount_unit == "contract" else Decimal(1)
            step, minimum = Decimal(str(self.amount_step)), Decimal(str(self.min_amount))
            if not all(x.is_finite() and x > 0 for x in (value, size, step, minimum)):
                raise InvalidOperation
            with localcontext() as context:
                context.traps[Inexact] = True
                result = value / size
                if result < minimum or result % step != 0:
                    raise InvalidOperation
                if self.market_min_amount is not None and result < Decimal(str(self.market_min_amount)):
                    raise InvalidOperation
                if self.market_amount_step and result % Decimal(str(self.market_amount_step)) != 0:
                    raise InvalidOperation
                return result
        except (DecimalException, ValueError, ZeroDivisionError) as exc:
            raise InvalidOrderError("Amount is invalid, below minimum or not an exact quantity step") from exc

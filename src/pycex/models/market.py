"""Unified market metadata model."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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


def select_markets(markets: list[Market], symbols: Sequence[str] | None) -> list[Market]:
    """``fetch_markets(symbols=)`` filter: ``None`` keeps all, otherwise only the listed canonical symbols."""
    if symbols is None:
        return markets
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of symbols, not a single string")
    wanted = set(symbols)
    return [m for m in markets if m.symbol in wanted]


TICK_LADDER_KEY = "price_tick_ladder"


def _positive_decimal(value: Any, what: str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a decimal, got {value!r}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{what} must be a decimal, got {value!r}") from None
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{what} must be {'non-negative' if allow_zero else 'positive'} and finite, got {value!r}")
    return number


def tick_ladder(market: Market) -> tuple[tuple[Decimal, Decimal], ...] | None:
    """Price-tier tick ladder from ``public_rules["price_tick_ladder"]``, or ``None``.

    The rule is a list of ``{"min_price": "<decimal>", "tick": "<decimal>"}``
    (exact decimal strings, like the other ``public_rules`` values). Returns
    ``(min_price, tick)`` pairs sorted ascending by floor as an immutable tuple;
    ``min_price`` is inclusive. Absent, ``None`` or empty -> ``None``. Malformed
    or duplicate-floor rules raise ``ValueError``.
    """
    raw = market.public_rules.get(TICK_LADDER_KEY)
    if raw is None or (isinstance(raw, list) and not raw):
        return None
    if not isinstance(raw, list):
        raise ValueError(f"{TICK_LADDER_KEY} must be a list of tiers, got {type(raw).__name__}")
    tiers: list[tuple[Decimal, Decimal]] = []
    for tier in raw:
        if not isinstance(tier, dict) or "min_price" not in tier or "tick" not in tier:
            raise ValueError(f"{TICK_LADDER_KEY} tier needs min_price and tick, got {tier!r}")
        tiers.append(
            (
                _positive_decimal(tier["min_price"], "min_price", allow_zero=True),
                _positive_decimal(tier["tick"], "tick", allow_zero=False),
            )
        )
    tiers.sort(key=lambda t: t[0])
    floors = [floor for floor, _ in tiers]
    if len(set(floors)) != len(floors):
        raise ValueError(f"{TICK_LADDER_KEY} has duplicate min_price")
    return tuple(tiers)


def tick_for_price(market: Market, price: Decimal | float | str) -> Decimal | None:
    """Price increment that applies at ``price``.

    With a ladder (see ``tick_ladder``) the tier with the greatest
    ``min_price <= price`` applies: a floor is inclusive, so a price exactly on
    it takes that tier and anything below it the tier before. Without a ladder
    this is the scalar ``price_tick`` (as ``Decimal``), or ``None`` when the
    market has neither. A non-finite or non-positive price raises
    ``ValueError`` on either path, as does a price below the first floor.
    """
    value = _positive_decimal(price, "price", allow_zero=False)
    ladder = tick_ladder(market)
    if ladder is None:
        return None if market.price_tick is None else Decimal(str(market.price_tick))
    applicable = None
    for floor, tick in ladder:
        if floor > value:
            break
        applicable = tick
    if applicable is None:
        raise ValueError(f"price {value} is below the first tier floor {ladder[0][0]}")
    return applicable

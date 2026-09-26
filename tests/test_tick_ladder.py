"""PX-LADDER — tick ladder in ``public_rules`` and ``tick_for_price``.

Boundary rule under test: a tier's ``min_price`` is inclusive, so a price
exactly on a floor uses that tier's tick and one unit below uses the tier before.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pycex.models import Market, tick_for_price, tick_ladder

# Illustrative ladder, not any venue's real values (those land per exchange).
_LADDER = [
    {"min_price": "2000000", "tick": "1000"},
    {"min_price": "0", "tick": "0.01"},  # deliberately unsorted
    {"min_price": "1", "tick": "0.1"},
    {"min_price": "1000", "tick": "1"},
]


def _market(*, price_tick: float | None = None, ladder: object = None) -> Market:
    rules = {} if ladder is None else {"price_tick_ladder": ladder}
    return Market(
        symbol="BTC/KRW",
        native="KRW-BTC",
        base="BTC",
        quote="KRW",
        market_type="spot",
        price_tick=price_tick,
        public_rules=rules,
    )


@pytest.mark.parametrize(
    ("price", "tick"),
    [
        ("0.5", "0.01"),
        ("0.99999999", "0.01"),  # just below a floor -> lower tier
        ("1", "0.1"),  # exactly on a floor -> that tier
        ("999.9", "0.1"),
        ("1000", "1"),
        ("1999999", "1"),
        ("2000000", "1000"),
        ("2000000.5", "1000"),
        ("10000000000000000000", "1000"),  # very large -> top tier
    ],
)
def test_ladder_boundaries(price: str, tick: str) -> None:
    assert tick_for_price(_market(ladder=_LADDER), Decimal(price)) == Decimal(tick)


def test_accepts_int_float_and_str_prices() -> None:
    m = _market(ladder=_LADDER)
    assert tick_for_price(m, 1000) == Decimal("1")
    assert tick_for_price(m, 999.5) == Decimal("0.1")
    assert tick_for_price(m, "2000000") == Decimal("1000")


@pytest.mark.parametrize("price", [0, -1, Decimal("-0.01"), float("nan"), float("inf"), "abc", None])
def test_invalid_price_raises(price: object) -> None:
    for m in (_market(ladder=_LADDER), _market(price_tick=0.5)):
        with pytest.raises(ValueError):
            tick_for_price(m, price)  # type: ignore[arg-type]


def test_price_below_first_floor_raises() -> None:
    m = _market(ladder=[{"min_price": "10", "tick": "1"}])
    with pytest.raises(ValueError, match="below"):
        tick_for_price(m, Decimal("9.99"))
    assert tick_for_price(m, Decimal("10")) == Decimal("1")


def test_no_ladder_falls_back_to_scalar_price_tick() -> None:
    assert tick_for_price(_market(price_tick=0.01), 12345) == Decimal("0.01")
    assert tick_for_price(_market(price_tick=1e-08), 1) == Decimal("1E-8")
    assert tick_ladder(_market(price_tick=0.01)) is None


def test_ladder_wins_over_scalar() -> None:
    assert tick_for_price(_market(price_tick=0.5, ladder=_LADDER), 5000) == Decimal("1")


@pytest.mark.parametrize("ladder", [[], None])
def test_empty_ladder_is_absent(ladder: object) -> None:
    m = _market(price_tick=0.25)
    m.public_rules = {"price_tick_ladder": ladder}
    assert tick_ladder(m) is None
    assert tick_for_price(m, 3) == Decimal("0.25")


def test_no_ladder_and_no_scalar_is_none() -> None:
    assert tick_for_price(_market(), 100) is None
    with pytest.raises(ValueError):
        tick_for_price(_market(), 0)


def test_tick_ladder_is_sorted_immutable_decimals() -> None:
    ladder = tick_ladder(_market(ladder=_LADDER))
    assert ladder == (
        (Decimal("0"), Decimal("0.01")),
        (Decimal("1"), Decimal("0.1")),
        (Decimal("1000"), Decimal("1")),
        (Decimal("2000000"), Decimal("1000")),
    )
    assert isinstance(ladder, tuple)


def test_source_rules_not_mutated_and_json_roundtrip() -> None:
    m = _market(ladder=list(_LADDER))
    before = [dict(t) for t in m.public_rules["price_tick_ladder"]]
    tick_ladder(m)
    assert m.public_rules["price_tick_ladder"] == before
    again = Market.model_validate_json(m.model_dump_json())
    assert tick_for_price(again, 1500) == Decimal("1")


@pytest.mark.parametrize(
    "ladder",
    [
        "not-a-list",
        [{"min_price": "0"}],  # missing tick
        [{"tick": "1"}],  # missing floor
        [{"min_price": "0", "tick": "0"}],  # zero tick
        [{"min_price": "0", "tick": "-1"}],
        [{"min_price": "-1", "tick": "1"}],
        [{"min_price": "x", "tick": "1"}],
        [{"min_price": "0", "tick": "1"}, {"min_price": "0", "tick": "2"}],  # duplicate floor
        [{"min_price": "NaN", "tick": "1"}],
        ["0:1"],
    ],
)
def test_malformed_ladder_raises(ladder: object) -> None:
    with pytest.raises(ValueError):
        tick_for_price(_market(ladder=ladder), 1)


def test_existing_price_tick_consumers_unchanged() -> None:
    m = _market(price_tick=0.01, ladder=_LADDER)
    assert m.price_tick == 0.01
    assert "price_tick_ladder" in m.public_rules
    assert _market().public_rules == {}

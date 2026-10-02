from decimal import Decimal

import pytest

from feasibility.markets.loader import get_pack
from feasibility.markets.schema import Sizing
from feasibility.proforma.sizing import buildable_size

CONFIG = get_pack("dallas").cost_assumptions.sizing


def test_a_known_zoning_uses_its_rule() -> None:
    choice = buildable_size(Decimal("6400"), "R-7.5(A)", CONFIG)

    assert choice.rule_used == "R-7.5(A)"
    assert choice.flags == ()
    sizing = choice.sizing
    assert (sizing.coverage_pct, sizing.stories, sizing.living_share_pct) == (45, 2, 55)
    assert sizing.footprint_sqft == Decimal("2880")
    assert sizing.gross_sqft == Decimal("5760")
    assert sizing.uncapped_sqft == Decimal("3168.00")
    assert sizing.buildable_sqft == Decimal("3168")
    assert sizing.capped_by == "none"


def test_a_large_lot_is_capped_at_the_maximum() -> None:
    choice = buildable_size(Decimal("10134"), "R-7.5(A)", CONFIG)

    assert choice.sizing.uncapped_sqft == Decimal("5016.33")
    assert choice.sizing.buildable_sqft == Decimal("3500")
    assert choice.sizing.capped_by == "max"
    assert choice.flags == ("size_capped_max",)


def test_a_small_lot_is_raised_to_the_minimum() -> None:
    choice = buildable_size(Decimal("2000"), "R-7.5(A)", CONFIG)

    assert choice.sizing.uncapped_sqft == Decimal("990.00")
    assert choice.sizing.buildable_sqft == Decimal("1500")
    assert choice.sizing.capped_by == "min"
    assert choice.flags == ("size_capped_min",)


def test_a_zoning_without_a_rule_uses_the_default_and_is_flagged() -> None:
    choice = buildable_size(Decimal("6200"), "CD-12", CONFIG)

    assert choice.rule_used == "default"
    assert choice.flags == ("zoning_rule_assumed",)
    assert choice.sizing.uncapped_sqft == Decimal("2480.00")
    assert choice.sizing.buildable_sqft == Decimal("2480")


@pytest.mark.parametrize("zoning", [None, "", "   "])
def test_a_missing_zoning_uses_the_default_and_is_flagged(zoning: str | None) -> None:
    choice = buildable_size(Decimal("6200"), zoning, CONFIG)

    assert choice.rule_used == "default"
    assert "zoning_rule_assumed" in choice.flags


def test_the_zoning_is_matched_after_upper_casing_and_removing_spaces() -> None:
    choice = buildable_size(Decimal("6400"), " r-7.5 (a) ", CONFIG)

    assert choice.rule_used == "R-7.5(A)"
    assert choice.flags == ()


def test_a_size_exactly_at_a_limit_is_not_capped() -> None:
    config = Sizing(
        min_home_sqft=Decimal("1000"),
        max_home_sqft=Decimal("2000"),
        default=CONFIG.default,
        rules={},
    )
    # default rule: 40 % x 2 stories x 50 % = 0.4 of the lot
    assert buildable_size(Decimal("5000"), None, config).sizing.capped_by == "none"
    assert buildable_size(Decimal("2500"), None, config).sizing.capped_by == "none"


def test_the_buildable_size_rounds_half_up_to_whole_feet() -> None:
    # 6405 x .45 x 2 x .55 = 3170.475 -> 3170; 6403 x .495 = 3169.485 -> 3169
    assert buildable_size(Decimal("6405"), "R-7.5(A)", CONFIG).sizing.buildable_sqft == 3170
    assert buildable_size(Decimal("6403"), "R-7.5(A)", CONFIG).sizing.buildable_sqft == 3169

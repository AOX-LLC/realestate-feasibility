from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from feasibility.markets.loader import get_pack
from feasibility.sourcing.filters import ListingFacts
from feasibility.sourcing.matching import MatchedParcel, MatchResult
from feasibility.sourcing.scoring import (
    RankKey,
    ScoreBreakdown,
    drift_land_value,
    rank_order,
    score_candidate,
)

PACK = get_pack("dallas")
DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
VALUES_DATE = date(2026, 1, 1)
MATCH = MatchResult("matched", "exact", (), 1, "key")


def _score(
    *,
    price: int,
    land: int,
    total: int,
    lot: int | None,
    built: int | None,
    property_type: str = "Single Family",
    as_of: date = DAY_TWO,
    listing_lot: int | None = None,
    values_date: date | None = VALUES_DATE,
) -> ScoreBreakdown:
    listing = ListingFacts(
        zip5="75214",
        property_type=property_type,
        price=Decimal(price),
        lot_size_sqft=None if listing_lot is None else Decimal(listing_lot),
        year_built=None,
    )
    parcel = MatchedParcel(
        account_id="1",
        gis_parcel_id=None,
        land_value=Decimal(land),
        improvement_value=Decimal(total - land),
        total_value=Decimal(total),
        year_built=built,
        lot_size_sqft=None if lot is None else Decimal(lot),
        values_file_date=values_date,
    )
    return score_candidate(PACK, as_of, listing, parcel, MATCH)


def _points(breakdown: ScoreBreakdown) -> list[str]:
    return [str(component.points) for component in breakdown.components]


@pytest.mark.parametrize(
    ("kwargs", "points", "total"),
    [
        # Vacant lot: account 051.
        (
            {
                "price": 378000,
                "land": 322000,
                "total": 322000,
                "lot": 10134,
                "built": None,
                "property_type": "Land",
            },
            ["35.00", "20.00", "13.78", "20.62"],
            "89.40",
        ),
        (
            {
                "price": 378000,
                "land": 322000,
                "total": 322000,
                "lot": 10134,
                "built": None,
                "property_type": "Land",
                "as_of": DAY_ONE,
            },
            ["35.00", "20.00", "13.78", "20.61"],
            "89.39",
        ),
        # Account 004 after its price cut.
        (
            {"price": 321000, "land": 248000, "total": 319000, "lot": 12904, "built": 1927},
            ["26.53", "20.00", "20.00", "16.75"],
            "83.28",
        ),
        # Account 006.
        (
            {"price": 517000, "land": 299000, "total": 398000, "lot": 14413, "built": 1948},
            ["23.48", "13.60", "20.00", "2.78"],
            "59.86",
        ),
        # Account 009: the price component earns nothing.
        (
            {"price": 576000, "land": 289000, "total": 481000, "lot": 8318, "built": 1951},
            ["5.93", "11.20", "7.73", "0.00"],
            "24.86",
        ),
    ],
)
def test_scores_match_the_formula(kwargs: dict[str, Any], points: list[str], total: str) -> None:
    breakdown = _score(**kwargs)
    assert _points(breakdown) == points
    assert str(breakdown.total) == total
    assert breakdown.total == sum(component.points for component in breakdown.components)


def test_drift_moves_land_value_forward_from_january_first() -> None:
    drifted = drift_land_value(Decimal(100000), date(2026, 9, 15), DAY_ONE, PACK)
    assert str(drifted.age_years) == "0.7474"
    assert str(drifted.factor) == "1.037370"
    assert str(drifted.adjusted_land_value) == "103737.00"
    assert not drifted.is_stale


def test_drift_is_capped_and_flags_old_values_as_stale() -> None:
    drifted = drift_land_value(Decimal(100000), date(2024, 6, 1), DAY_TWO, PACK)
    assert drifted.age_years == Decimal(2)
    assert str(drifted.factor) == "1.100000"
    assert drifted.is_stale


def test_values_dated_after_the_run_do_not_drift_backwards() -> None:
    drifted = drift_land_value(Decimal(100000), date(2027, 1, 1), DAY_TWO, PACK)
    assert drifted.age_years == Decimal(0)
    assert drifted.adjusted_land_value == Decimal("100000.00")


def test_unknown_values_date_means_no_drift_and_no_flag() -> None:
    drifted = drift_land_value(Decimal(100000), None, DAY_TWO, PACK)
    assert drifted.factor == Decimal(1)
    assert not drifted.is_stale


def test_stale_values_are_flagged_in_the_breakdown_but_still_scored() -> None:
    breakdown = _score(
        price=400000, land=250000, total=400000, lot=8000, built=1950, values_date=date(2024, 1, 1)
    )
    assert breakdown.values.flags == ["values_stale"]
    assert breakdown.total > 0


def test_ratio_clamps_to_zero_and_full_points() -> None:
    below = _score(price=300000, land=100, total=1000, lot=8000, built=1950)
    above = _score(price=300000, land=950, total=1000, lot=8000, built=1950)
    assert below.components[0].points == Decimal("0.00")
    assert above.components[0].points == Decimal("35.00")


def test_missing_lot_everywhere_scores_zero_and_is_reported() -> None:
    breakdown = _score(price=300000, land=250000, total=300000, lot=None, built=1950)
    assert breakdown.components[2].points == Decimal("0.00")
    assert breakdown.missing == ["lot_size"]


def test_inputs_that_fell_back_to_the_listing_are_named() -> None:
    breakdown = _score(
        price=355000, land=250000, total=300000, lot=None, built=1958, listing_lot=10250
    )
    assert breakdown.inputs_from_listing == ["lot_size"]
    assert breakdown.missing == []
    assert str(breakdown.components[2].points) == "14.17"


def test_a_vacant_lot_earns_the_vacant_age_credit() -> None:
    breakdown = _score(
        price=300000, land=300000, total=300000, lot=8000, built=None, property_type="Land"
    )
    assert breakdown.components[1].points == Decimal("20.00")
    assert breakdown.missing == []


def test_breakdown_serialises_decimals_as_strings_with_stable_keys() -> None:
    body = _score(price=321000, land=248000, total=319000, lot=12904, built=1927).model_dump(
        mode="json"
    )
    assert list(body) == [
        "version",
        "total",
        "match",
        "values",
        "components",
        "inputs_from_listing",
        "missing",
    ]
    assert body["total"] == "83.28"
    assert [component["name"] for component in body["components"]] == [
        "land_ratio",
        "age",
        "lot",
        "price_vs_land",
    ]
    assert body["values"]["drift_factor"] == "1.037510"


def test_rank_order_is_score_then_price_then_candidate_id() -> None:
    entries = [
        RankKey(candidate_id=3, score=Decimal("50.00"), price=Decimal(300000)),
        RankKey(candidate_id=1, score=Decimal("60.00"), price=Decimal(500000)),
        RankKey(candidate_id=2, score=Decimal("50.00"), price=Decimal(200000)),
        RankKey(candidate_id=5, score=Decimal("50.00"), price=Decimal(300000)),
        RankKey(candidate_id=4, score=Decimal("50.00"), price=Decimal(300000)),
    ]
    assert rank_order(entries) == [1, 2, 3, 4, 5]

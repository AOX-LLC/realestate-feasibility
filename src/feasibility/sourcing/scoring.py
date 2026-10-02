"""The teardown score: four components that add up to 100, on the market pack's numbers.

Everything here is pure and uses Decimal. Each component's points are rounded to 0.01 and
the score is the sum of the rounded points, so a breakdown always adds up to its total.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from feasibility.markets.schema import MarketPack
from feasibility.sourcing.filters import (
    ListingFacts,
    is_vacant,
    lot_size_of,
    year_built_of,
)
from feasibility.sourcing.matching import MatchedParcel, MatchResult

POINTS_STEP = Decimal("0.01")
FRACTION_STEP = Decimal("0.0001")
AGE_STEP = Decimal("0.0001")
FACTOR_STEP = Decimal("0.000001")
DAYS_PER_YEAR = Decimal("365.25")

COMPONENT_NAMES = ("land_ratio", "age", "lot", "price_vs_land")


class BreakdownModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class MatchSummary(BreakdownModel):
    status: str
    method: str | None
    account_count: int


class ValuesUsed(BreakdownModel):
    land_value: Decimal
    improvement_value: Decimal | None
    total_value: Decimal
    values_file_date: date | None
    values_as_of: date | None
    values_age_years: Decimal
    drift_factor: Decimal
    adjusted_land_value: Decimal
    flags: list[Literal["values_stale"]]


class Component(BreakdownModel):
    name: str
    input_label: str
    input: Decimal | None
    fraction: Decimal
    weight: Decimal
    points: Decimal


class ScoreBreakdown(BreakdownModel):
    """Stored as run_candidate.breakdown and returned by the API. Later phases read this
    JSON, so its keys do not change."""

    version: Literal[1]
    total: Decimal
    match: MatchSummary
    values: ValuesUsed
    components: list[Component]
    inputs_from_listing: list[Literal["lot_size", "year_built"]]
    missing: list[Literal["lot_size", "year_built"]]


@dataclass(frozen=True, slots=True)
class DriftedLand:
    values_as_of: date | None
    age_years: Decimal
    factor: Decimal
    adjusted_land_value: Decimal
    is_stale: bool


def clamp(value: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), value))


def quantize_points(points: Decimal) -> Decimal:
    return points.quantize(POINTS_STEP, rounding=ROUND_HALF_UP)


def drift_land_value(
    land_value: Decimal, values_file_date: date | None, as_of: date, pack: MarketPack
) -> DriftedLand:
    """Move the land value forward from January 1 of its roll year to `as_of`.

    Simple growth, capped at `max_drift_years`. Only the price-versus-land component uses
    the result; a uniform market move cancels in the ratio components.
    """
    scoring = pack.sourcing.scoring
    if values_file_date is None:
        return DriftedLand(None, Decimal(0), Decimal(1), land_value, is_stale=False)

    values_as_of = date(values_file_date.year, 1, 1)
    elapsed_years = Decimal((as_of - values_as_of).days) / DAYS_PER_YEAR
    age_years = max(Decimal(0), min(elapsed_years, scoring.max_drift_years))
    age_years = age_years.quantize(AGE_STEP, rounding=ROUND_HALF_UP)

    factor = (Decimal(1) + scoring.value_drift_pct_per_year / 100 * age_years).quantize(
        FACTOR_STEP, rounding=ROUND_HALF_UP
    )
    adjusted = (land_value * factor).quantize(POINTS_STEP, rounding=ROUND_HALF_UP)
    return DriftedLand(
        values_as_of, age_years, factor, adjusted, is_stale=age_years > scoring.stale_values_years
    )


def _component(
    name: str, input_label: str, value: Decimal | None, fraction: Decimal, weight: Decimal
) -> Component:
    return Component(
        name=name,
        input_label=input_label,
        input=value,
        fraction=fraction.quantize(FRACTION_STEP, rounding=ROUND_HALF_UP),
        weight=weight,
        points=quantize_points(weight * fraction),
    )


def score_candidate(
    pack: MarketPack,
    as_of: date,
    listing: ListingFacts,
    parcel: MatchedParcel,
    match: MatchResult,
) -> ScoreBreakdown:
    """Score a matched candidate whose parcel has usable values (`has_usable_values`)."""
    if parcel.land_value is None or parcel.total_value is None or parcel.total_value <= 0:
        raise ValueError("score_candidate needs a parcel with usable values")
    if listing.price is None:
        raise ValueError("score_candidate needs a listing price")

    box, scoring = pack.buy_box, pack.sourcing.scoring
    drifted = drift_land_value(parcel.land_value, parcel.values_file_date, as_of, pack)
    lot = lot_size_of(listing, parcel)
    year_built = year_built_of(listing, parcel)
    vacant = is_vacant(listing, parcel)

    land_ratio = parcel.land_value / parcel.total_value
    land_fraction = clamp(
        (land_ratio - box.land_to_total_min) / (scoring.land_ratio_full - box.land_to_total_min)
    )

    if vacant:
        age_fraction = scoring.vacant_age_credit
    elif year_built is None:
        age_fraction = Decimal(0)
    else:
        age_fraction = clamp(
            Decimal(box.year_built_max - year_built)
            / Decimal(box.year_built_max - scoring.age_full_year)
        )

    lot_fraction = (
        Decimal(0)
        if lot is None
        else clamp((lot - box.lot_size_min_sqft) / (scoring.lot_full_sqft - box.lot_size_min_sqft))
    )

    price_to_land = (
        listing.price / drifted.adjusted_land_value if drifted.adjusted_land_value > 0 else None
    )
    price_fraction = (
        Decimal(0)
        if price_to_land is None
        else clamp(
            (scoring.price_land_zero - price_to_land)
            / (scoring.price_land_zero - scoring.price_land_full)
        )
    )

    components = [
        _component(
            "land_ratio",
            "land_value / total_value",
            land_ratio.quantize(FRACTION_STEP),
            land_fraction,
            scoring.land_ratio_weight,
        ),
        _component(
            "age",
            "year_built (vacant lot: none)",
            None if year_built is None else Decimal(year_built),
            age_fraction,
            scoring.age_weight,
        ),
        _component("lot", "lot_size_sqft", lot, lot_fraction, scoring.lot_weight),
        _component(
            "price_vs_land",
            "price / adjusted_land_value",
            None if price_to_land is None else price_to_land.quantize(FRACTION_STEP),
            price_fraction,
            scoring.price_land_weight,
        ),
    ]
    return ScoreBreakdown(
        version=1,
        total=sum((component.points for component in components), Decimal(0)),
        match=MatchSummary(
            status=match.status, method=match.method, account_count=match.account_count
        ),
        values=ValuesUsed(
            land_value=parcel.land_value,
            improvement_value=parcel.improvement_value,
            total_value=parcel.total_value,
            values_file_date=parcel.values_file_date,
            values_as_of=drifted.values_as_of,
            values_age_years=drifted.age_years,
            drift_factor=drifted.factor,
            adjusted_land_value=drifted.adjusted_land_value,
            flags=["values_stale"] if drifted.is_stale else [],
        ),
        components=components,
        inputs_from_listing=_inputs_from_listing(listing, parcel),
        missing=_missing_inputs(lot, year_built, vacant),
    )


def _inputs_from_listing(
    listing: ListingFacts, parcel: MatchedParcel
) -> list[Literal["lot_size", "year_built"]]:
    used: list[Literal["lot_size", "year_built"]] = []
    if parcel.lot_size_sqft is None and listing.lot_size_sqft is not None:
        used.append("lot_size")
    if parcel.year_built is None and listing.year_built is not None:
        used.append("year_built")
    return used


def _missing_inputs(
    lot: Decimal | None, year_built: int | None, vacant: bool
) -> list[Literal["lot_size", "year_built"]]:
    missing: list[Literal["lot_size", "year_built"]] = []
    if lot is None:
        missing.append("lot_size")
    if year_built is None and not vacant:
        missing.append("year_built")
    return missing


@dataclass(frozen=True, slots=True)
class RankKey:
    candidate_id: int
    score: Decimal
    price: Decimal


def rank_order(entries: Sequence[RankKey]) -> list[int]:
    """Candidate ids best first: higher score, then lower price, then lower candidate id."""
    ordered = sorted(entries, key=lambda e: (-e.score, e.price, e.candidate_id))
    return [entry.candidate_id for entry in ordered]

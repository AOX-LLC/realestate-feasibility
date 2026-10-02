"""Inputs and results of the pro-forma.

Every model is frozen and rejects unknown fields; every number is a Decimal and serialises as a
string (`model_dump(mode="json")`), so a stored result reads back to exactly the same value.
`ProformaResult` is stored as `proforma.result`: Phase 4 and 5 read its keys, so they do not change.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict

from feasibility.markets.schema import CostAssumptions


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("use a Decimal or a string, never a float")
    return value


Dec = Annotated[Decimal, BeforeValidator(_reject_float)]

Status = Literal["computed", "no_arv", "unsizable"]
CappedBy = Literal["max", "min", "none"]
LotSource = Literal["parcel", "listing"]
ExistingSqftSource = Literal["parcel", "assumed", "vacant"]


class ProformaModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- inputs ---------------------------------------------------------------------------------


class Comp(ProformaModel):
    address: str
    price: Dec
    living_area_sqft: int | None
    distance_miles: Dec | None
    year_built: int | None


class EstimateInput(ProformaModel):
    fetched_on: date
    outcome: Literal["ok", "no_estimate"]
    price: Dec | None
    comps: tuple[Comp, ...] = ()


class ProformaInputs(ProformaModel):
    price: Dec  # the primary listing's price in this run
    lot_sqft: Dec | None
    lot_source: LotSource
    zoning: str | None
    zoning_values_seen: tuple[str, ...] = ()
    is_vacant: bool
    is_gis_group: bool
    existing_living_sqft: Dec | None
    as_of: date
    estimate: EstimateInput | None


# --- results --------------------------------------------------------------------------------


class Site(ProformaModel):
    lot_sqft: Dec | None
    lot_source: LotSource
    zoning: str | None
    zoning_values_seen: tuple[str, ...]
    rule_used: str | None  # the zoning rule's name, or "default"; None when nothing was sized
    is_vacant: bool
    is_gis_group: bool
    existing_living_sqft: Dec | None
    existing_sqft_source: ExistingSqftSource


class SizingResult(ProformaModel):
    coverage_pct: Dec
    stories: int
    living_share_pct: Dec
    footprint_sqft: Dec
    gross_sqft: Dec
    uncapped_sqft: Dec
    min_home_sqft: Dec
    max_home_sqft: Dec
    buildable_sqft: Dec
    capped_by: CappedBy


class CompLine(ProformaModel):
    address: str
    price: Dec
    living_area_sqft: int | None
    psf: Dec | None  # price per square foot, 4 places; None for a comp that was not used
    used: bool


class ArvDetail(ProformaModel):
    estimate_fetched_on: date
    estimate_stale: bool
    estimate_price: Dec | None  # the AVM point estimate of the existing property; context only
    comp_count_used: int
    comp_count_dropped: int
    comps: tuple[CompLine, ...]
    median_psf: Dec | None
    premium_pct: Dec
    arv: Dec | None


class Costs(ProformaModel):
    offer_price: Dec
    acquisition_closing: Dec
    demolition: Dec
    hard_cost: Dec
    contingency: Dec
    soft_costs: Dec


class FinancingMonths(ProformaModel):
    hold: Dec
    construction: Dec
    sale: Dec


class FinancingResult(ProformaModel):
    months: FinancingMonths
    financeable_cost: Dec
    loan_amount: Dec
    interest_front: Dec
    interest_progressive: Dec
    interest: Dec
    points: Dec
    draw_fees: Dec
    total: Dec


class HoldingResult(ProformaModel):
    property_tax: Dec
    insurance: Dec
    total: Dec


class SellingResult(ProformaModel):
    commission: Dec
    closing: Dec
    total: Dec


class Totals(ProformaModel):
    total_cost: Dec
    profit: Dec
    margin: Dec  # profit / ARV, a fraction
    cash_invested: Dec
    roi: Dec | None  # None when no cash is invested
    annualized_return: Dec | None


class MaxOffer(ProformaModel):
    target_margin: Dec  # a fraction of ARV, like `margin`
    fixed_part: Dec  # unrounded
    price_coefficient: Dec  # unrounded
    max_offer: Dec | None  # None when the target margin is unreachable at any price
    headroom_vs_offer: Dec | None


class SensitivityAxes(ProformaModel):
    arv_delta_pct: tuple[Dec, ...]
    hard_cost_delta_pct: tuple[Dec, ...]
    hold_months: tuple[Dec, ...]


class SensitivityCell(ProformaModel):
    arv_delta_pct: Dec
    hard_cost_delta_pct: Dec
    hold_months: Dec
    arv: Dec
    hard_cost: Dec
    total_cost: Dec
    profit: Dec
    margin: Dec
    roi: Dec | None


class SensitivityTable(ProformaModel):
    axes: SensitivityAxes
    cells: tuple[SensitivityCell, ...]


class CostChain(ProformaModel):
    """Lines 9-28 of the formulas for one price, hard cost, ARV and hold."""

    costs: Costs
    arv: Dec | None
    financing: FinancingResult
    holding: HoldingResult
    selling: SellingResult | None  # needs the ARV
    totals: Totals | None  # needs the ARV


class ProformaResult(ProformaModel):
    version: Literal[1] = 1
    status: Status
    reason: str | None
    flags: tuple[str, ...]
    assumptions: CostAssumptions
    site: Site
    sizing: SizingResult | None
    arv: ArvDetail | None
    costs: Costs | None
    financing: FinancingResult | None
    holding: HoldingResult | None
    selling: SellingResult | None
    totals: Totals | None
    max_offer: MaxOffer | None
    sensitivity: SensitivityTable | None

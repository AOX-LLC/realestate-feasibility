"""Response bodies. Every endpoint returns one of these explicit shapes, never a raw row.
Money is a decimal string so no precision is lost in JSON."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from feasibility.sourcing.counts import RunCounts
from feasibility.sourcing.scoring import ScoreBreakdown


class ResponseModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Health(ResponseModel):
    status: str
    commit: str | None
    commit_source: str
    branch: str | None
    version: str
    schema_version: str | None
    uptime_s: int
    mode: str
    rentcast_key_configured: bool


class MarketSummary(ResponseModel):
    id: str
    name: str
    state: str
    county: str
    timezone: str


class BuyBoxOut(ResponseModel):
    zips: list[str]
    price_max: Decimal
    lot_size_min_sqft: Decimal
    year_built_max: int
    land_to_total_min: Decimal
    property_types: list[str]


class ScoringOut(ResponseModel):
    land_ratio_weight: Decimal
    land_ratio_full: Decimal
    age_weight: Decimal
    age_full_year: int
    lot_weight: Decimal
    lot_full_sqft: Decimal
    price_land_weight: Decimal
    price_land_full: Decimal
    price_land_zero: Decimal
    vacant_age_credit: Decimal
    value_drift_pct_per_year: Decimal
    max_drift_years: Decimal
    stale_values_years: Decimal


class EstimatesOut(ResponseModel):
    top_n: int
    monthly_cap: int
    ttl_days: int
    sync_reserve_per_day: int


class SourcingOut(ResponseModel):
    source_priority: list[str]
    scoring: ScoringOut
    estimates: EstimatesOut


class MarketDetail(MarketSummary):
    parcel_source: str
    listing_sources: list[str]
    buy_box: BuyBoxOut
    sourcing: SourcingOut
    cost_assumptions_status: str


class ParcelOut(ResponseModel):
    market: str
    account_id: str
    gis_parcel_id: str | None
    street_number: str | None
    street_half: str | None
    street_name: str | None
    unit: str | None
    city: str | None
    zip5: str | None
    land_value: Decimal | None
    improvement_value: Decimal | None
    total_value: Decimal | None
    year_built: int | None
    living_area_sqft: int | None
    lot_size_sqft: Decimal | None
    use_code: str | None
    zoning: str | None
    attrs_file_date: date
    values_file_date: date | None


class ListingOut(ResponseModel):
    id: int
    source: str
    external_id: str
    market: str
    address_line: str
    city: str | None
    state: str | None
    zip5: str | None
    price: Decimal | None
    status: str | None
    property_type: str | None
    lot_size_sqft: Decimal | None
    living_area_sqft: int | None
    year_built: int | None
    listed_date: date | None
    remarks: str | None
    first_seen_at: datetime
    last_seen_at: datetime


class JobOut(ResponseModel):
    id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    has_error: bool
    run_after: datetime
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class BudgetOut(ResponseModel):
    provider: str
    mode: str
    period_start: date
    limit: int
    used: int
    remaining: int


class RunOut(ResponseModel):
    id: int
    market: str
    as_of: date
    status: str
    sync_status: str
    counts: RunCounts
    started_at: datetime
    finished_at: datetime | None


class CandidateAddressOut(ResponseModel):
    street: str
    zip5: str | None


class MatchOut(ResponseModel):
    status: str
    method: str | None


class RunCandidateOut(ResponseModel):
    candidate_id: int
    rank: int | None
    score: Decimal | None
    price: Decimal | None
    address: CandidateAddressOut
    change_kind: str
    status: Literal["ranked", "filtered", "unscored"]
    filter_reasons: list[str]
    unscored_reason: str | None
    match: MatchOut | None
    account_id: str | None


class CandidateListingOut(ResponseModel):
    listing_id: int
    source: str
    price: Decimal | None
    prev_price: Decimal | None
    change_kind: str
    is_primary: bool


class CandidateDetailOut(RunCandidateOut):
    # Null unless the candidate is ranked.
    breakdown: ScoreBreakdown | None
    listings: list[CandidateListingOut]


class Page[T](ResponseModel):
    items: list[T]
    # Pass as `after` to get the next page; null on the last page.
    next_after: str | None

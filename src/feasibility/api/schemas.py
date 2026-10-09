"""Response bodies. Every endpoint returns one of these explicit shapes, never a raw row.
Money is a decimal string so no precision is lost in JSON."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from feasibility.proforma.model import ArvDetail, ProformaResult
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


class EstimateOut(ResponseModel):
    """A stored value estimate. The comparables stay in the database."""

    fetched_on: date
    outcome: Literal["ok", "no_estimate"]
    # Null when RentCast had no estimate for the address.
    price: Decimal | None
    price_low: Decimal | None
    price_high: Decimal | None
    comp_count: int
    dropped_comp_count: int


class CandidateDetailOut(RunCandidateOut):
    # Null unless the candidate is ranked.
    breakdown: ScoreBreakdown | None
    listings: list[CandidateListingOut]
    # The newest estimate bought on or before the run's date; null when there is none.
    estimate: EstimateOut | None


class ProformaSummaryOut(ResponseModel):
    """The figures of a pro-forma that a list shows. Everything but the ids, rank and address
    is null when the status says there was nothing to compute it from."""

    candidate_id: int
    rank: int
    address: CandidateAddressOut
    status: Literal["computed", "no_arv", "unsizable"]
    # Why a pro-forma is not computed; null when it is.
    reason: str | None
    flags: list[str]
    # The price modelled: the primary listing's price in that run.
    offer_price: Decimal
    arv: Decimal | None
    total_cost: Decimal | None
    profit: Decimal | None
    # Ratios, not percents: 0.1964 is 19.64%.
    margin: Decimal | None
    roi: Decimal | None
    annualized_return: Decimal | None
    # The most that earns the target margin; null when no price does.
    max_offer: Decimal | None


class CompLineOut(ResponseModel):
    """A comparable sale as the API shows it: its price, size and price per square foot, never
    its address (the addresses stay in the database, like an estimate's comparables)."""

    price: Decimal
    living_area_sqft: int | None
    psf: Decimal | None
    used: bool


class ArvDetailOut(ArvDetail):
    comps: tuple[CompLineOut, ...]  # type: ignore[assignment]


class ProformaResultOut(ProformaResult):
    """The stored result with the comparables' addresses left out."""

    arv: ArvDetailOut | None

    @classmethod
    def without_addresses(cls, result: ProformaResult) -> "ProformaResultOut":
        data = result.model_dump(mode="json")
        if data["arv"] is not None:
            for comp in data["arv"]["comps"]:
                del comp["address"]
        return cls.model_validate(data)


class ProformaDetailOut(ProformaSummaryOut):
    """The summary and the full result: every assumption, input and line, but not the comps'
    addresses."""

    result: ProformaResultOut


class Page[T](ResponseModel):
    items: list[T]
    # Pass as `after` to get the next page; null on the last page.
    next_after: str | None


# --- the model stages (read-only) ---------------------------------------------------------------
# A signal's quote is a stretch of the listing's redacted remarks that code verified, the same
# text `GET /listings` already serves. A narrative is served as the model wrote it only when it
# was accepted; a rejected one is its status, its reason and the kinds of rule it broke, never
# the draft and never the text of a violation (a violation holds a few characters of the draft).


class RemarksReadOut(ResponseModel):
    char_count: int
    redaction_count: int
    removed_invisible_count: int
    suspicious: bool
    suspicious_rules: list[str]


class SignalOut(ResponseModel):
    code: str
    polarity: Literal["risk", "opportunity"]
    source: Literal["remarks", "fields"]
    quote: str | None
    field: str | None
    field_value: str | None


class DroppedClaimOut(ResponseModel):
    code: str
    reason: str


class ExtractionOut(ResponseModel):
    prompt_id: str
    prompt_version: int
    tier: str
    reused: bool
    llm_call_id: int | None


class SignalsOut(ResponseModel):
    status: Literal["extracted", "fields_only", "failed", "deferred"]
    reason: str | None
    remarks: RemarksReadOut | None
    signals: list[SignalOut]
    dropped: list[DroppedClaimOut]
    model_flagged_injection: bool | None
    extraction: ExtractionOut | None


class RiskPointOut(ResponseModel):
    basis: list[str]
    text: str


class QuotedFigureOut(ResponseModel):
    key: str
    text: str


class NarrativeCheckOut(ResponseModel):
    passed: bool
    attempts: int
    # Which rules the draft broke, once per violation. Never what the draft said.
    violation_kinds: list[str]


class NarrativeFactsOut(ResponseModel):
    figures: dict[str, str]
    codes: list[str]


class NarrativeModelOut(ResponseModel):
    prompt_id: str
    prompt_version: int
    tier: str
    reused: bool
    llm_call_ids: list[int]


class NarrativeOut(ResponseModel):
    status: Literal["accepted", "rejected", "failed", "deferred", "not_eligible"]
    reason: str | None
    summary: str | None
    risks: list[RiskPointOut]
    checks_before_offer: list[str]
    figures_quoted: list[QuotedFigureOut]
    check: NarrativeCheckOut | None
    facts: NarrativeFactsOut
    model: NarrativeModelOut | None


class CandidateLlmOut(ResponseModel):
    run_id: int
    candidate_id: int
    signals: SignalsOut | None
    narrative: NarrativeOut | None


class NarrativeLineOut(ResponseModel):
    candidate_id: int
    rank: int
    status: Literal["accepted", "rejected", "failed", "deferred", "not_eligible"]
    reason: str | None
    summary: str | None


class CostLineOut(ResponseModel):
    key: str | None
    calls: int
    refused_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    reserved_unknown_usd: Decimal


class RunCostOut(ResponseModel):
    run_id: int
    run_budget_usd: Decimal
    # What the cap counts: the known costs, plus the reservation held for each call that raised.
    spent_usd: Decimal
    remaining_usd: Decimal
    modes: list[str]
    total: CostLineOut
    by_stage: list[CostLineOut]
    by_model: list[CostLineOut]


class MonthSpendOut(ResponseModel):
    month: str
    billable_calls: int
    refused_calls: int
    cost_usd: Decimal
    reserved_unknown_usd: Decimal
    spent_usd: Decimal
    monthly_budget_usd: Decimal
    remaining_usd: Decimal

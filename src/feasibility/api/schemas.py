"""Response bodies. Every endpoint returns one of these explicit shapes, never a raw row.
Money is a decimal string so no precision is lost in JSON."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


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


class MarketDetail(MarketSummary):
    parcel_source: str
    listing_sources: list[str]
    buy_box: BuyBoxOut
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


class Page[T](ResponseModel):
    items: list[T]
    # Pass as `after` to get the next page; null on the last page.
    next_after: str | None

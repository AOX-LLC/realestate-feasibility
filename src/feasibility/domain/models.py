from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from feasibility.domain.address import Address


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Parcel(DomainModel):
    """A county appraisal account: where it is, what is on it and what it is worth."""

    market: str
    account_id: str
    gis_parcel_id: str | None = None
    address: Address
    land_value: Decimal | None = None
    improvement_value: Decimal | None = None
    total_value: Decimal | None = None
    year_built: int | None = None
    living_area_sqft: int | None = None
    lot_size_sqft: Decimal | None = None
    use_code: str | None = None
    zoning: str | None = None


class Listing(DomainModel):
    """A property for sale, from any listing source."""

    source: str
    external_id: str
    address: Address
    price: Decimal | None = None
    status: str | None = None
    property_type: str | None = None
    lot_size_sqft: Decimal | None = None
    living_area_sqft: int | None = None
    year_built: int | None = None
    listed_date: date | None = None
    # Free-text description. MLS feeds carry it (RESO PublicRemarks); RentCast does not.
    remarks: str | None = None
    # The source record with personal fields already removed.
    raw: dict[str, Any] = Field(default_factory=dict)


class PropertyRecord(DomainModel):
    """Public-record facts about a property, without any owner information."""

    source: str
    external_id: str
    address: Address
    property_type: str | None = None
    lot_size_sqft: Decimal | None = None
    living_area_sqft: int | None = None
    year_built: int | None = None
    zoning: str | None = None
    last_sale_date: date | None = None
    last_sale_price: Decimal | None = None


class SaleComp(DomainModel):
    """A comparable sale listing used to support a value estimate."""

    address: Address
    price: Decimal
    living_area_sqft: int | None = None
    lot_size_sqft: Decimal | None = None
    year_built: int | None = None
    distance_miles: Decimal | None = None
    days_old: int | None = None
    correlation: Decimal | None = None


class ValueEstimate(DomainModel):
    source: str
    address: Address
    price: Decimal
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    comparables: tuple[SaleComp, ...] = ()
    # Comparables the source returned that were not sale listings, left out above.
    dropped_comparables: int = 0

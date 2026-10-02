"""RentCast response models: the one schema source of truth for live responses and for the
synthetic snapshot (scripts/generate_snapshot.py builds its JSON from these classes).

Written from RentCast's published OpenAPI 3.1 definition, read 2026-10-02:
    https://developers.rentcast.io/openapi/rentcast-api.json
    https://developers.rentcast.io/reference/sale-listings
    https://developers.rentcast.io/reference/property-listings-schema
    https://developers.rentcast.io/reference/property-valuation
scripts/check_rentcast_docs.py re-fetches the definition and diffs its field names
against these models.

The published schema marks no response field as required. The fields this application
keys on are required here (id and formattedAddress; price on a value estimate), so a
response without them fails as shape drift instead of loading as junk. Unknown fields
are kept (extra="allow") so a new live field survives into the stored raw record.

Personal fields (listingAgent, listingOffice, owner) are deliberately not declared:
scrub.py removes them before anything is validated, cached or stored.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class RentCastModel(BaseModel):
    model_config = ConfigDict(extra="allow", alias_generator=to_camel, populate_by_name=True)


class Hoa(RentCastModel):
    fee: float | None = None


class AddressedRecord(RentCastModel):
    """Fields shared by listings, property records and comparables."""

    id: str
    formatted_address: str
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    state_fips: str | None = None
    zip_code: str | None = None
    county: str | None = None
    county_fips: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    property_type: str | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    square_footage: int | None = None
    lot_size: float | None = None
    year_built: int | None = None


class SaleListingHistoryEvent(RentCastModel):
    event: str | None = None
    price: float | None = None
    listing_type: str | None = None
    listed_date: datetime | None = None
    removed_date: datetime | None = None
    days_on_market: int | None = None


class SaleListing(AddressedRecord):
    """An item of GET /listings/sale, and the body of GET /listings/sale/{id}."""

    hoa: Hoa | None = None
    status: str | None = None
    price: float | None = None
    listing_type: str | None = None
    listed_date: datetime | None = None
    removed_date: datetime | None = None
    created_date: datetime | None = None
    last_seen_date: datetime | None = None
    days_on_market: int | None = None
    mls_name: str | None = None
    mls_number: str | None = None
    history: dict[str, SaleListingHistoryEvent] | None = None


class PropertyFeatures(RentCastModel):
    architecture_type: str | None = None
    cooling: bool | None = None
    cooling_type: str | None = None
    exterior_type: str | None = None
    fireplace: bool | None = None
    fireplace_type: str | None = None
    floor_count: int | None = None
    foundation_type: str | None = None
    garage: bool | None = None
    garage_spaces: int | None = None
    garage_type: str | None = None
    heating: bool | None = None
    heating_type: str | None = None
    pool: bool | None = None
    pool_type: str | None = None
    roof_type: str | None = None
    room_count: int | None = None
    unit_count: int | None = None
    view_type: str | None = None


class TaxAssessment(RentCastModel):
    year: int | None = None
    value: float | None = None
    land: float | None = None
    improvements: float | None = None


class PropertyTax(RentCastModel):
    year: int | None = None
    total: float | None = None


class PropertyHistoryEvent(RentCastModel):
    event: str | None = None
    date: datetime | None = None
    price: float | None = None


class PropertyRecord(AddressedRecord):
    """An item of GET /properties, and the body of GET /properties/{id}."""

    assessor_id: str | None = Field(default=None, alias="assessorID")
    legal_description: str | None = None
    subdivision: str | None = None
    zoning: str | None = None
    last_sale_date: datetime | None = None
    last_sale_price: float | None = None
    hoa: Hoa | None = None
    features: PropertyFeatures | None = None
    tax_assessments: dict[str, TaxAssessment] | None = None
    property_taxes: dict[str, PropertyTax] | None = None
    history: dict[str, PropertyHistoryEvent] | None = None
    owner_occupied: bool | None = None


class SubjectProperty(AddressedRecord):
    last_sale_date: datetime | None = None
    last_sale_price: float | None = None


class Comparable(AddressedRecord):
    status: str | None = None
    price: float | None = None
    listing_type: str | None = None
    listed_date: datetime | None = None
    removed_date: datetime | None = None
    last_seen_date: datetime | None = None
    days_on_market: int | None = None
    distance: float | None = None
    days_old: int | None = None
    correlation: float | None = None


class ValueEstimate(RentCastModel):
    """The body of GET /avm/value."""

    price: float
    price_range_low: float | None = None
    price_range_high: float | None = None
    subject_property: SubjectProperty | None = None
    comparables: list[Comparable] = Field(default_factory=list)

"""Maps RentCast responses onto the domain models."""

from decimal import Decimal

from feasibility.domain import Address, Listing, PropertyRecord, SaleComp, ValueEstimate
from feasibility.sources.base import ListingBatch, ListingQuery
from feasibility.sources.rentcast import models
from feasibility.sources.rentcast.client import RentCastClient

SOURCE = "rentcast"
# RentCast's documented sale listing types. /avm/value comparables carry no explicit
# sale/rent flag, so only comps of a known sale type are kept; the rest are counted.
SALE_LISTING_TYPES = frozenset({"Standard", "New Construction", "Foreclosure", "Short Sale"})


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _address(record: models.AddressedRecord) -> Address:
    return Address.normalized(
        record.address_line1 or record.formatted_address.split(",")[0],
        unit=record.address_line2,
        city=record.city,
        state=record.state,
        zip_code=record.zip_code,
    )


def to_listing(record: models.SaleListing) -> Listing:
    return Listing(
        source=SOURCE,
        external_id=record.id,
        address=_address(record),
        price=_decimal(record.price),
        status=record.status,
        property_type=record.property_type,
        lot_size_sqft=_decimal(record.lot_size),
        living_area_sqft=record.square_footage,
        year_built=record.year_built,
        listed_date=record.listed_date.date() if record.listed_date else None,
        # RentCast listings have no description text; MLS feeds supply remarks.
        remarks=None,
        raw=record.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )


def to_property_record(record: models.PropertyRecord) -> PropertyRecord:
    return PropertyRecord(
        source=SOURCE,
        external_id=record.id,
        address=_address(record),
        property_type=record.property_type,
        lot_size_sqft=_decimal(record.lot_size),
        living_area_sqft=record.square_footage,
        year_built=record.year_built,
        zoning=record.zoning,
        last_sale_date=record.last_sale_date.date() if record.last_sale_date else None,
        last_sale_price=_decimal(record.last_sale_price),
    )


def to_value_estimate(estimate: models.ValueEstimate, address: Address) -> ValueEstimate:
    sale_comps = [
        comp
        for comp in estimate.comparables
        if comp.listing_type in SALE_LISTING_TYPES and comp.price is not None
    ]
    return ValueEstimate(
        source=SOURCE,
        address=address,
        price=Decimal(str(estimate.price)),
        price_low=_decimal(estimate.price_range_low),
        price_high=_decimal(estimate.price_range_high),
        comparables=tuple(
            SaleComp(
                address=_address(comp),
                price=Decimal(str(comp.price)),
                living_area_sqft=comp.square_footage,
                lot_size_sqft=_decimal(comp.lot_size),
                year_built=comp.year_built,
                distance_miles=_decimal(comp.distance),
                days_old=comp.days_old,
                correlation=_decimal(comp.correlation),
            )
            for comp in sale_comps
        ),
        dropped_comparables=len(estimate.comparables) - len(sale_comps),
    )


class RentCastListingSource:
    name = SOURCE

    def __init__(self, client: RentCastClient) -> None:
        self._client = client

    def fetch_listings(self, query: ListingQuery) -> ListingBatch:
        fetched = self._client.sale_listings(query)
        return ListingBatch([to_listing(record) for record in fetched.data], fetched.stale)


class RentCastPropertySource:
    name = SOURCE

    def __init__(self, client: RentCastClient) -> None:
        self._client = client

    def fetch_property(self, address: Address) -> PropertyRecord | None:
        record = self._client.property_record(address.one_line).data
        return None if record is None else to_property_record(record)


class RentCastValuationSource:
    name = SOURCE

    def __init__(self, client: RentCastClient) -> None:
        self._client = client

    def estimate_value(self, address: Address) -> ValueEstimate | None:
        estimate = self._client.value_estimate(address.one_line).data
        return None if estimate is None else to_value_estimate(estimate, address)

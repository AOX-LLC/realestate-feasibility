"""The buy box as pure functions.

Two stages. Listing-level filters need nothing but the listing, so they run before
matching and a failing listing is never matched. Parcel-level filters run on a matched
parcel's values and collect every reason that applies.
"""

from dataclasses import dataclass
from decimal import Decimal

from feasibility.markets.schema import BuyBox
from feasibility.sourcing.matching import MatchedParcel

LAND_TYPE = "Land"


@dataclass(frozen=True, slots=True)
class ListingFacts:
    """The listing fields the filters and the score read."""

    zip5: str | None
    property_type: str | None
    price: Decimal | None
    lot_size_sqft: Decimal | None
    year_built: int | None


def listing_filter_reason(listing: ListingFacts, box: BuyBox) -> str | None:
    """The first listing-level reason this listing fails the buy box, or None."""
    if listing.zip5 not in box.zips:
        return "zip"
    if listing.property_type not in box.property_types:
        return "property_type"
    if listing.price is None or listing.price > box.price_max:
        return "price"
    return None


def lot_size_of(listing: ListingFacts, parcel: MatchedParcel) -> Decimal | None:
    """The county's lot size, else the listing's."""
    return parcel.lot_size_sqft if parcel.lot_size_sqft is not None else listing.lot_size_sqft


def year_built_of(listing: ListingFacts, parcel: MatchedParcel) -> int | None:
    """The county's year built, else the listing's."""
    return parcel.year_built if parcel.year_built is not None else listing.year_built


def is_vacant(listing: ListingFacts, parcel: MatchedParcel) -> bool:
    """A Land listing with no year built anywhere."""
    return listing.property_type == LAND_TYPE and year_built_of(listing, parcel) is None


def has_usable_values(parcel: MatchedParcel) -> bool:
    """Land and total value present and a positive total: the minimum to judge and score."""
    return (
        parcel.land_value is not None and parcel.total_value is not None and parcel.total_value > 0
    )


def parcel_filter_reasons(listing: ListingFacts, parcel: MatchedParcel, box: BuyBox) -> list[str]:
    """Every parcel-level reason this candidate fails the buy box, in a fixed order.

    The parcel must have usable values (`has_usable_values`); a parcel without them is
    unscored, not filtered.
    """
    if parcel.land_value is None or parcel.total_value is None or parcel.total_value <= 0:
        raise ValueError("parcel_filter_reasons needs a parcel with usable values")

    reasons: list[str] = []

    lot = lot_size_of(listing, parcel)
    if lot is None or lot < box.lot_size_min_sqft:
        reasons.append("lot_size")

    year_built = year_built_of(listing, parcel)
    if year_built is None:
        if listing.property_type != LAND_TYPE:
            reasons.append("year_built")
    elif year_built > box.year_built_max:
        reasons.append("year_built")

    if parcel.land_value / parcel.total_value < box.land_to_total_min:
        reasons.append("land_to_total")

    return reasons

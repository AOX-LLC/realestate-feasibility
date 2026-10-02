from decimal import Decimal
from typing import Any

import pytest

from feasibility.markets.loader import get_pack
from feasibility.sourcing.filters import (
    ListingFacts,
    has_usable_values,
    listing_filter_reason,
    parcel_filter_reasons,
)
from feasibility.sourcing.matching import MatchedParcel

BOX = get_pack("dallas").buy_box


def _listing(**overrides: Any) -> ListingFacts:
    facts: dict[str, Any] = {
        "zip5": "75214",
        "property_type": "Single Family",
        "price": Decimal(400000),
        "lot_size_sqft": None,
        "year_built": None,
    }
    return ListingFacts(**{**facts, **overrides})


def _parcel(**overrides: Any) -> MatchedParcel:
    values: dict[str, Any] = {
        "account_id": "1",
        "gis_parcel_id": None,
        "land_value": Decimal(300000),
        "improvement_value": Decimal(100000),
        "total_value": Decimal(400000),
        "year_built": 1950,
        "lot_size_sqft": Decimal(8000),
        "values_file_date": None,
    }
    return MatchedParcel(**{**values, **overrides})


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"zip5": "75001"}, "zip"),
        ({"zip5": None}, "zip"),
        ({"property_type": "Condo"}, "property_type"),
        ({"property_type": "Land"}, None),
        ({"price": Decimal(650000)}, None),
        ({"price": Decimal(650001)}, "price"),
        ({"price": None}, "price"),
        ({"zip5": "75001", "property_type": "Condo", "price": None}, "zip"),
        ({"property_type": "Condo", "price": None}, "property_type"),
    ],
)
def test_listing_level_filter_reports_the_first_failure(
    overrides: dict[str, Any], reason: str | None
) -> None:
    assert listing_filter_reason(_listing(**overrides), BOX) == reason


@pytest.mark.parametrize(
    ("listing", "parcel", "reasons"),
    [
        (_listing(), _parcel(), []),
        # Boundaries: lot 6000, year 1965 and a 0.55 ratio all pass.
        (
            _listing(),
            _parcel(
                lot_size_sqft=Decimal(6000),
                year_built=1965,
                land_value=Decimal(55),
                total_value=Decimal(100),
            ),
            [],
        ),
        (_listing(), _parcel(lot_size_sqft=Decimal("5999.99")), ["lot_size"]),
        (_listing(), _parcel(year_built=1966), ["year_built"]),
        (
            _listing(),
            _parcel(land_value=Decimal("54.99"), total_value=Decimal(100)),
            ["land_to_total"],
        ),
        # The listing's lot size stands in for a missing parcel lot size.
        (
            _listing(lot_size_sqft=Decimal(7000)),
            _parcel(lot_size_sqft=None),
            [],
        ),
        (
            _listing(lot_size_sqft=Decimal(5000)),
            _parcel(lot_size_sqft=None),
            ["lot_size"],
        ),
        (_listing(), _parcel(lot_size_sqft=None), ["lot_size"]),
        # A missing year passes only for Land.
        (_listing(property_type="Land"), _parcel(year_built=None), []),
        (_listing(), _parcel(year_built=None), ["year_built"]),
        (_listing(year_built=1950), _parcel(year_built=None), []),
        # Every failing reason is collected, in order.
        (
            _listing(),
            _parcel(
                lot_size_sqft=Decimal(100),
                year_built=1990,
                land_value=Decimal(10),
                total_value=Decimal(100),
            ),
            ["lot_size", "year_built", "land_to_total"],
        ),
    ],
)
def test_parcel_level_filter_collects_every_reason(
    listing: ListingFacts, parcel: MatchedParcel, reasons: list[str]
) -> None:
    assert parcel_filter_reasons(listing, parcel, BOX) == reasons


@pytest.mark.parametrize(
    ("overrides", "usable"),
    [
        ({}, True),
        ({"total_value": None}, False),
        ({"land_value": None}, False),
        ({"total_value": Decimal(0)}, False),
    ],
)
def test_usable_values_need_land_and_a_positive_total(
    overrides: dict[str, Any], usable: bool
) -> None:
    assert has_usable_values(_parcel(**overrides)) is usable


def test_parcel_filters_refuse_a_parcel_without_values() -> None:
    with pytest.raises(ValueError, match="usable values"):
        parcel_filter_reasons(_listing(), _parcel(total_value=None), BOX)

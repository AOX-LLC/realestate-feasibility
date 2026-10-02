from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from feasibility.proforma.model import (
    Comp,
    EstimateInput,
    ProformaInputs,
)


def make_inputs(**changes: object) -> ProformaInputs:
    fields: dict[str, object] = {
        "price": Decimal("420000"),
        "lot_sqft": Decimal("6400"),
        "lot_source": "parcel",
        "zoning": "R-7.5(A)",
        "zoning_values_seen": ("R-7.5(A)",),
        "is_vacant": False,
        "is_gis_group": False,
        "existing_living_sqft": Decimal("1150"),
        "as_of": date(2026, 10, 2),
        "estimate": EstimateInput(
            fetched_on=date(2026, 10, 1),
            outcome="ok",
            price=Decimal("410000"),
            comps=(
                Comp(
                    address="1 Main St",
                    price=Decimal("1330000"),
                    living_area_sqft=3000,
                    distance_miles=Decimal("0.40"),
                    year_built=1998,
                ),
            ),
        ),
    }
    fields.update(changes)
    return ProformaInputs.model_validate(fields)


def test_inputs_are_frozen() -> None:
    inputs = make_inputs()
    with pytest.raises(ValidationError):
        inputs.price = Decimal(1)  # type: ignore[misc]


def test_inputs_reject_unknown_fields_and_floats_for_money() -> None:
    with pytest.raises(ValidationError):
        make_inputs(owner_name="nobody")
    with pytest.raises(ValidationError):
        make_inputs(price=420000.5)


def test_decimals_serialise_as_strings() -> None:
    dumped = make_inputs().model_dump(mode="json")
    assert dumped["price"] == "420000"
    assert dumped["estimate"]["comps"][0]["distance_miles"] == "0.40"

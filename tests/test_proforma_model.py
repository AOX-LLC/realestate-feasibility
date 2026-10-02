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


def test_a_price_must_be_positive_and_in_whole_cents() -> None:
    for bad in (Decimal(0), Decimal("-100000"), Decimal("420000.005")):
        with pytest.raises(ValidationError):
            make_inputs(price=bad)
    assert make_inputs(price=Decimal("420000.50")).price == Decimal("420000.50")


def test_an_estimate_fetched_after_the_as_of_date_is_rejected() -> None:
    future = EstimateInput(fetched_on=date(2026, 10, 3), outcome="ok", price=Decimal(1), comps=())
    with pytest.raises(ValidationError, match="after"):
        make_inputs(estimate=future)


def test_absurdly_large_amounts_are_rejected_before_they_reach_the_arithmetic() -> None:
    with pytest.raises(ValidationError):
        make_inputs(price=Decimal("1E26"))
    with pytest.raises(ValidationError):
        Comp(
            address="1 Main St",
            price=Decimal("1E26"),
            living_area_sqft=3000,
            distance_miles=None,
            year_built=None,
        )

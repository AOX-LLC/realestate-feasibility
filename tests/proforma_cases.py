"""Shared pro-forma test inputs: the three scenarios of the formula section (F.7)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from feasibility.markets.loader import get_pack
from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.model import Comp, EstimateInput, ProformaInputs

AS_OF = date(2026, 10, 2)
PACK = get_pack("dallas")
ASSUMPTIONS: CostAssumptions = PACK.cost_assumptions
TTL_DAYS = PACK.sourcing.estimates.ttl_days

S1_COMPS = [
    (1330000, 3000),
    (1420000, 3150),
    (1250000, 2800),
    (1480000, 3300),
    (1190000, 2650),
    (1390000, 3100),
    (1520000, 3350),
]
S2_COMPS = [(1450000, 3400), (1380000, 3250), (1500000, 3600), (1280000, 3000)]
S3_COMPS = [(640000, 2300), (690000, 2500), (610000, 2250), (720000, 2700)]


def comps(pairs: list[tuple[int, int | None]]) -> tuple[Comp, ...]:
    return tuple(
        Comp(
            address=f"{number} Comp St",
            price=Decimal(price),
            living_area_sqft=area,
            distance_miles=Decimal("0.50"),
            year_built=1998,
        )
        for number, (price, area) in enumerate(pairs, start=1)
    )


def estimate(
    pairs: list[tuple[int, int | None]],
    *,
    age_days: int = 1,
    outcome: str = "ok",
) -> EstimateInput:
    return EstimateInput.model_validate(
        {
            "fetched_on": AS_OF - timedelta(days=age_days),
            "outcome": outcome,
            "price": Decimal("400000") if outcome == "ok" else None,
            "comps": comps(pairs) if outcome == "ok" else (),
        }
    )


def inputs(**changes: Any) -> ProformaInputs:
    """Scenario S1 (a house on a mid lot) unless changed."""
    fields: dict[str, Any] = {
        "price": Decimal("420000"),
        "lot_sqft": Decimal("6400"),
        "lot_source": "parcel",
        "zoning": "R-7.5(A)",
        "zoning_values_seen": ("R-7.5(A)",),
        "is_vacant": False,
        "is_gis_group": False,
        "existing_living_sqft": Decimal("1150"),
        "as_of": AS_OF,
        "estimate": estimate(S1_COMPS),
    }
    fields.update(changes)
    return ProformaInputs.model_validate(fields)


def s1() -> ProformaInputs:
    return inputs()


def s2() -> ProformaInputs:
    return inputs(
        price=Decimal("300000"),
        lot_sqft=Decimal("10134"),
        is_vacant=True,
        existing_living_sqft=None,
        estimate=estimate(S2_COMPS),
    )


def s3() -> ProformaInputs:
    return inputs(
        price=Decimal("495000"),
        lot_sqft=Decimal("6200"),
        zoning="CD-12",
        zoning_values_seen=("CD-12",),
        existing_living_sqft=None,
        estimate=estimate(S3_COMPS),
    )

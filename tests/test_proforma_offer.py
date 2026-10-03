from decimal import Decimal

import pytest

from feasibility.markets.loader import get_pack
from feasibility.proforma.chain import cost_chain
from feasibility.proforma.model import CostChain
from feasibility.proforma.offer import max_offer

ASSUMPTIONS = get_pack("dallas").cost_assumptions
HOLD = ASSUMPTIONS.holding.hold_months
TARGET = ASSUMPTIONS.target.margin_pct / 100

S1 = ("420000", "11700.00", "601920.00", "1420799.85")
S2 = ("300000", "0.00", "665000.00", "1489400.50")
S3 = ("495000", "14500.00", "471200.00", "678417.76")


def base(case: tuple[str, str, str, str]) -> CostChain:
    price, demolition, hard, arv = case
    return cost_chain(
        Decimal(price), Decimal(demolition), Decimal(hard), Decimal(arv), HOLD, ASSUMPTIONS
    )


@pytest.mark.parametrize(
    ("case", "fixed_part", "offer", "headroom"),
    [
        (S1, "850105.5804", "324271.50", "-95728.50"),
        (S2, "920363.8400", "313436.54", "13436.54"),
        (S3, "645421.2140", None, None),
    ],
    ids=["S1", "S2", "S3"],
)
def test_the_closed_form_matches_the_formula_section(
    case: tuple[str, str, str, str], fixed_part: str, offer: str | None, headroom: str | None
) -> None:
    result = max_offer(base(case), ASSUMPTIONS)

    assert result.target_margin == Decimal("0.15")
    assert abs(result.fixed_part - Decimal(fixed_part)) < Decimal("0.000001")
    assert abs(result.price_coefficient - Decimal("1.102700325")) < Decimal("0.000001")
    if offer is None:
        assert result.max_offer is None
        assert result.headroom_vs_offer is None
    else:
        assert result.max_offer == Decimal(offer)
        assert result.headroom_vs_offer == Decimal(headroom)  # type: ignore[arg-type]


def test_the_maximum_offer_is_a_whole_number_of_cents() -> None:
    offer = max_offer(base(S1), ASSUMPTIONS).max_offer

    assert offer is not None
    assert offer == offer.quantize(Decimal("0.01"))


@pytest.mark.parametrize("case", [S1, S2], ids=["S1", "S2"])
def test_at_the_maximum_offer_profit_is_the_target_within_five_cents(
    case: tuple[str, str, str, str],
) -> None:
    start = base(case)
    offer = max_offer(start, ASSUMPTIONS).max_offer
    assert offer is not None and start.arv is not None

    at_max = cost_chain(
        offer, start.costs.demolition, start.costs.hard_cost, start.arv, HOLD, ASSUMPTIONS
    )

    assert at_max.totals is not None
    target_profit = (start.arv * TARGET).quantize(Decimal("0.01"))
    assert abs(at_max.totals.profit - target_profit) <= Decimal("0.05")


@pytest.mark.parametrize("case", [S1, S2], ids=["S1", "S2"])
def test_one_dollar_more_misses_the_target(case: tuple[str, str, str, str]) -> None:
    start = base(case)
    offer = max_offer(start, ASSUMPTIONS).max_offer
    assert offer is not None and start.arv is not None

    above = cost_chain(
        offer + 1, start.costs.demolition, start.costs.hard_cost, start.arv, HOLD, ASSUMPTIONS
    )

    assert above.totals is not None
    assert above.totals.profit < start.arv * TARGET


def test_a_chain_without_an_arv_has_no_maximum_offer_to_compute() -> None:
    no_arv = cost_chain(
        Decimal("420000"), Decimal("11700"), Decimal("601920"), None, HOLD, ASSUMPTIONS
    )

    with pytest.raises(ValueError, match="ARV"):
        max_offer(no_arv, ASSUMPTIONS)

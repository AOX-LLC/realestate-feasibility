from decimal import Decimal

import pytest

from feasibility.markets.loader import get_pack
from feasibility.proforma.chain import cost_chain
from feasibility.proforma.model import CostChain

ASSUMPTIONS = get_pack("dallas").cost_assumptions
HOLD = ASSUMPTIONS.holding.hold_months

# price, demolition, hard cost, ARV: the inputs of the three hand-computed scenarios
S1 = ("420000", "11700.00", "601920.00", "1420799.85")
S2 = ("300000", "0.00", "665000.00", "1489400.50")
S3 = ("495000", "14500.00", "471200.00", "678417.76")


def chain(case: tuple[str, str, str, str], hold: Decimal = HOLD) -> CostChain:
    price, demolition, hard, arv = case
    return cost_chain(
        Decimal(price), Decimal(demolition), Decimal(hard), Decimal(arv), hold, ASSUMPTIONS
    )


def test_s1_costs_and_financing_match_the_formula_section() -> None:
    result = chain(S1)

    assert result.costs.acquisition_closing == Decimal("4200.00")
    assert result.costs.contingency == Decimal("30096.00")
    assert result.costs.soft_costs == Decimal("72230.40")
    financing = result.financing
    assert financing.months.construction == Decimal("6.3")
    assert financing.months.sale == Decimal("2.7")
    assert financing.financeable_cost == Decimal("1135946.40")
    assert financing.loan_amount == Decimal("908757.12")
    assert financing.interest_front == Decimal("30235.82")
    assert financing.interest_progressive == Decimal("24648.62")
    assert financing.interest == Decimal("54884.44")
    assert financing.points == Decimal("18175.14")
    assert str(financing.draw_fees) == "1000.00"  # stated in cents like every money line
    assert financing.total == Decimal("74059.58")


def test_s1_holding_selling_and_totals_match_the_formula_section() -> None:
    result = chain(S1)

    assert result.holding.property_tax == Decimal("7014.14")
    assert result.holding.insurance == Decimal("6771.60")
    assert result.holding.total == Decimal("13785.74")
    assert result.selling is not None
    assert result.selling.commission == Decimal("71039.99")
    assert result.selling.closing == Decimal("14208.00")
    assert result.selling.total == Decimal("85247.99")
    totals = result.totals
    assert totals is not None
    assert totals.total_cost == Decimal("1313239.71")
    assert totals.profit == Decimal("107560.14")
    assert totals.margin == Decimal("0.0757")
    assert totals.cash_invested == Decimal("319234.60")
    assert totals.roi == Decimal("0.3369")
    assert totals.annualized_return == Decimal("0.4492")


@pytest.mark.parametrize(
    ("case", "total_cost", "profit", "margin", "roi", "annualized"),
    [
        (S2, "1251173.94", "238226.56", "0.1599", "0.7958", "1.0610"),
        (S3, "1191257.87", "-512840.11", "-0.7559", "-1.6987", "-2.2649"),
    ],
    ids=["S2", "S3"],
)
def test_s2_and_s3_totals_match_the_formula_section(
    case: tuple[str, str, str, str],
    total_cost: str,
    profit: str,
    margin: str,
    roi: str,
    annualized: str,
) -> None:
    totals = chain(case).totals

    assert totals is not None
    assert totals.total_cost == Decimal(total_cost)
    assert totals.profit == Decimal(profit)
    assert totals.margin == Decimal(margin)
    assert totals.roi == Decimal(roi)
    assert totals.annualized_return == Decimal(annualized)


def test_without_an_arv_financing_and_holding_are_still_computed() -> None:
    price, demolition, hard, _ = S1
    result = cost_chain(Decimal(price), Decimal(demolition), Decimal(hard), None, HOLD, ASSUMPTIONS)

    assert result.arv is None
    assert result.selling is None
    assert result.totals is None
    assert result.financing.total == Decimal("74059.58")
    assert result.holding.total == Decimal("13785.74")


def test_every_money_line_is_a_whole_number_of_cents() -> None:
    result = chain(S1)
    lines = [
        *result.costs.model_dump().values(),
        result.financing.financeable_cost,
        result.financing.loan_amount,
        result.financing.interest,
        result.financing.points,
        result.financing.total,
        *result.holding.model_dump().values(),
        *(result.selling.model_dump().values() if result.selling else []),
        *(
            result.totals.model_dump(exclude={"margin", "roi", "annualized_return"}).values()
            if result.totals
            else []
        ),
    ]

    assert all(line == line.quantize(Decimal("0.01")) for line in lines)


def test_a_shorter_hold_has_fewer_months_and_less_interest() -> None:
    short = chain(S1, Decimal("6"))
    base = chain(S1)

    assert short.financing.months.construction == Decimal("4.2")
    assert short.financing.months.sale == Decimal("1.8")
    assert short.financing.interest < base.financing.interest


def test_no_cash_invested_leaves_roi_blank() -> None:
    # Every cost financed and nothing else charged: the builder has put in no cash.
    price, demolition, hard, arv = S1
    zero = Decimal(0)
    assumptions = ASSUMPTIONS.model_copy(
        update={
            "acquisition": ASSUMPTIONS.acquisition.model_copy(update={"closing_pct": zero}),
            "financing": ASSUMPTIONS.financing.model_copy(
                update={
                    "loan_to_cost_pct": Decimal(100),
                    "rate_pct": zero,
                    "points_pct": zero,
                    "draw_count": 0,
                }
            ),
            "holding": ASSUMPTIONS.holding.model_copy(
                update={"property_tax_rate_pct": zero, "insurance_pct_of_hard_cost_per_year": zero}
            ),
        }
    )
    result = cost_chain(
        Decimal(price), Decimal(demolition), Decimal(hard), Decimal(arv), HOLD, assumptions
    )

    assert result.totals is not None
    assert result.totals.cash_invested == 0
    assert result.totals.roi is None
    assert result.totals.annualized_return is None

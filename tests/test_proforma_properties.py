"""Properties of the pro-forma that hold for any inputs, checked without the workbook."""

import json
import re
import typing
from decimal import Decimal
from pathlib import Path

import pytest
from proforma_cases import ASSUMPTIONS, TTL_DAYS, inputs, s1, s2, s3
from pydantic import BaseModel

import feasibility.proforma as proforma_package
from feasibility.proforma import model
from feasibility.proforma.chain import cost_chain
from feasibility.proforma.engine import build_proforma
from feasibility.proforma.model import ProformaInputs, ProformaResult

CENT = Decimal("0.01")
SCENARIOS = [s1, s2, s3]
HOLD = ASSUMPTIONS.holding.hold_months


def run(case: ProformaInputs) -> ProformaResult:
    return build_proforma(case, ASSUMPTIONS, estimate_ttl_days=TTL_DAYS)


def computed(case: ProformaInputs) -> ProformaResult:
    result = run(case)
    assert result.status == "computed"
    return result


def money_lines(result: ProformaResult) -> list[Decimal]:
    """Every figure the result states in dollars (not ratios, sizes or the max-offer parts)."""
    assert result.arv and result.costs and result.financing and result.holding
    assert result.selling and result.totals and result.max_offer and result.sensitivity
    financing = result.financing.model_dump(exclude={"months"})
    totals = result.totals.model_dump(exclude={"margin", "roi", "annualized_return"})
    lines = [
        result.arv.arv,
        *(line.price for line in result.arv.comps),
        *result.costs.model_dump().values(),
        *financing.values(),
        *result.holding.model_dump().values(),
        *result.selling.model_dump().values(),
        *totals.values(),
        result.max_offer.max_offer,
        result.max_offer.headroom_vs_offer,
    ]
    for cell in result.sensitivity.cells:
        lines += [cell.arv, cell.hard_cost, cell.total_cost, cell.profit]
    return [line for line in lines if line is not None]


@pytest.mark.parametrize("case", SCENARIOS)
def test_profit_plus_total_cost_is_exactly_the_arv(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    result = computed(case())

    assert result.totals is not None and result.arv is not None
    assert result.totals.profit + result.totals.total_cost == result.arv.arv


@pytest.mark.parametrize("case", SCENARIOS)
def test_every_money_line_is_a_whole_number_of_cents(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    for line in money_lines(computed(case())):
        assert line == line.quantize(CENT), line


@pytest.mark.parametrize("case", SCENARIOS)
def test_the_lines_add_up_to_the_total_cost_exactly(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    result = computed(case())
    costs, financing, holding, selling = (
        result.costs,
        result.financing,
        result.holding,
        result.selling,
    )
    assert costs and financing and holding and selling and result.totals

    lines = [
        costs.offer_price,
        costs.acquisition_closing,
        costs.demolition,
        costs.hard_cost,
        costs.contingency,
        costs.soft_costs,
        financing.interest_front,
        financing.interest_progressive,
        financing.points,
        financing.draw_fees,
        holding.property_tax,
        holding.insurance,
        selling.commission,
        selling.closing,
    ]
    assert sum(lines) == result.totals.total_cost
    assert financing.interest == financing.interest_front + financing.interest_progressive
    assert financing.total == financing.interest + financing.points + financing.draw_fees
    assert holding.total == holding.property_tax + holding.insurance
    assert selling.total == selling.commission + selling.closing


def profit(price: str = "420000", hard: str = "601920.00", hold: Decimal = HOLD) -> Decimal:
    chain = cost_chain(
        Decimal(price), Decimal("11700.00"), Decimal(hard), Decimal("1420799.85"), hold, ASSUMPTIONS
    )
    assert chain.totals is not None
    return chain.totals.profit


def test_profit_falls_as_the_price_rises() -> None:
    profits = [profit(price=str(price)) for price in range(200_000, 700_001, 50_000)]

    assert profits == sorted(profits, reverse=True)
    assert len(set(profits)) == len(profits)


def test_profit_falls_as_the_hard_cost_rises() -> None:
    profits = [profit(hard=str(hard)) for hard in range(400_000, 900_001, 50_000)]

    assert profits == sorted(profits, reverse=True)
    assert len(set(profits)) == len(profits)


def test_profit_falls_as_the_hold_lengthens() -> None:
    profits = [profit(hold=Decimal(months)) for months in range(3, 25)]

    assert profits == sorted(profits, reverse=True)
    assert len(set(profits)) == len(profits)


@pytest.mark.parametrize("case", [s1, s2])
def test_the_maximum_offer_is_within_cents_of_the_target_and_a_dollar_more_misses(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    result = computed(case())
    assert result.max_offer and result.max_offer.max_offer and result.arv and result.arv.arv
    arv = result.arv.arv
    target_profit = (arv * result.max_offer.target_margin).quantize(CENT)

    at_max = computed(case().model_copy(update={"price": result.max_offer.max_offer}))
    above = computed(case().model_copy(update={"price": result.max_offer.max_offer + 1}))

    assert at_max.totals and above.totals
    assert abs(at_max.totals.profit - target_profit) <= Decimal("0.05")
    assert above.totals.profit < target_profit


@pytest.mark.parametrize("case", SCENARIOS)
def test_no_viable_offer_exactly_when_the_numerator_is_negative(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    result = computed(case())
    assert result.max_offer and result.arv and result.arv.arv is not None
    numerator = result.arv.arv * (1 - result.max_offer.target_margin) - result.max_offer.fixed_part

    assert (numerator < 0) == (result.max_offer.max_offer is None)
    assert (numerator < 0) == ("no_viable_offer" in result.flags)


@pytest.mark.parametrize("case", SCENARIOS)
def test_a_higher_arv_cell_beats_the_base_and_a_lower_one_loses_to_it(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    result = computed(case())
    assert result.sensitivity and result.totals
    by_point = {
        (c.arv_delta_pct, c.hard_cost_delta_pct, c.hold_months): c for c in result.sensitivity.cells
    }
    base = by_point[(Decimal(0), Decimal(0), HOLD)]
    better = by_point[(Decimal(10), Decimal(0), HOLD)]
    worse = by_point[(Decimal(-10), Decimal(0), HOLD)]

    assert base.profit == result.totals.profit
    assert better.profit > base.profit > worse.profit


@pytest.mark.parametrize("case", SCENARIOS)
def test_the_result_round_trips_through_json(case: typing.Callable[[], ProformaInputs]) -> None:
    result = computed(case())

    assert ProformaResult.model_validate(result.model_dump(mode="json")) == result
    assert ProformaResult.model_validate_json(result.model_dump_json()) == result


def test_every_status_round_trips_through_json() -> None:
    for case in (inputs(estimate=None), inputs(lot_sqft=None)):
        result = run(case)
        assert ProformaResult.model_validate(result.model_dump(mode="json")) == result


@pytest.mark.parametrize("case", SCENARIOS)
def test_the_same_inputs_give_byte_identical_json(
    case: typing.Callable[[], ProformaInputs],
) -> None:
    first = json.dumps(run(case()).model_dump(mode="json"), sort_keys=True)
    second = json.dumps(run(case()).model_dump(mode="json"), sort_keys=True)

    assert first == second


def annotated_types(annotation: object) -> typing.Iterator[object]:
    yield annotation
    for argument in typing.get_args(annotation):
        yield from annotated_types(argument)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for field in annotation.model_fields.values():
            yield from annotated_types(field.annotation)


def test_no_model_field_is_a_float() -> None:
    models = [
        value
        for value in vars(model).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == model.__name__
    ]
    assert len(models) > 15

    for pydantic_model in models:
        for field_name, field in pydantic_model.model_fields.items():
            found = set(annotated_types(field.annotation))
            assert float not in found, f"{pydantic_model.__name__}.{field_name}"


def test_the_proforma_source_never_calls_float_or_rounds_outside_money() -> None:
    sources = sorted(Path(proforma_package.__file__).parent.glob("*.py"))
    assert len(sources) >= 9

    for source in sources:
        text = source.read_text()
        assert not re.search(r"(?<![\w.])float\(", text), source.name
        if source.name != "money.py":
            assert "quantize(" not in text, source.name
            assert not re.search(r"(?<![\w.])round\(", text), source.name
            assert "ROUND_" not in text, source.name

"""Integrity and regression checks on docs/proforma-reference.xlsx.

The workbook is an independent check on the pro-forma formulas, so these tests do not
recompute anything: they pin its layout, keep its assumptions equal to the Dallas pack, and
hold its recalculated values to figures computed by hand from the pro-forma formulas.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from feasibility.markets.loader import get_pack
from feasibility.proforma.engine import build_proforma
from feasibility.proforma.model import Comp, EstimateInput, ProformaInputs, ProformaResult
from feasibility.proforma.money import round_money

WORKBOOK = Path(__file__).resolve().parents[1] / "docs" / "proforma-reference.xlsx"
SHEETS = ["Summary", "Assumptions", "S1", "S2", "S3", "Sensitivity"]
SCENARIOS = ["S1", "S2", "S3"]
KEY_COLUMN = 4  # D
VALUE_COLUMN = 2  # B
AT_MAX_VALUE_COLUMN = 5  # E
AT_MAX_KEY_COLUMN = 6  # F
# LibreOffice writes a boolean input back as a constant formula; nothing else may be a formula.
BOOLEAN_INPUT_FORMULAS = {"=TRUE()", "=FALSE()"}
CENT = Decimal("0.005")
RATIO = Decimal("0.00005")
EXACT_RATIO = RATIO / 10  # ratios in the workbook are rounded results, so compare tightly


@pytest.fixture(scope="module")
def formulas() -> Any:
    return load_workbook(WORKBOOK)


@pytest.fixture(scope="module")
def values() -> Any:
    return load_workbook(WORKBOOK, data_only=True)


def key_rows(sheet: Worksheet, key_column: int = KEY_COLUMN) -> dict[str, int]:
    rows: dict[str, int] = {}
    for row in range(2, sheet.max_row + 1):
        key = sheet.cell(row, key_column).value
        if key:
            assert key not in rows, f"{sheet.title}: duplicate key {key}"
            rows[key] = row
    return rows


def number(cell_value: object) -> Decimal:
    assert isinstance(cell_value, int | float | Decimal), f"not a number: {cell_value!r}"
    return Decimal(str(round(cell_value, 8)))


def test_the_workbook_has_the_sheets_in_the_layout_contract(formulas: Any) -> None:
    assert formulas.sheetnames == SHEETS


def test_the_workbook_has_no_hidden_sheets_or_defined_names(formulas: Any) -> None:
    assert all(sheet.sheet_state == "visible" for sheet in formulas)
    assert not list(formulas.defined_names)


def test_no_sheet_has_merged_cells_or_hidden_rows(formulas: Any) -> None:
    for sheet in formulas:
        assert not sheet.merged_cells.ranges, sheet.title
        assert not [row for row, dim in sheet.row_dimensions.items() if dim.hidden], sheet.title


# --- Assumptions equal the pack ------------------------------------------------------------

PACK_PATHS = {
    "as.acq_closing_pct": ("acquisition", "closing_pct"),
    "as.demo_flat": ("demolition", "flat"),
    "as.demo_per_sqft": ("demolition", "per_sqft"),
    "as.demo_fallback_sqft": ("demolition", "fallback_sqft"),
    "as.hard_cost_psf": ("construction", "hard_cost_per_sqft"),
    "as.contingency_pct": ("construction", "contingency_pct"),
    "as.soft_cost_pct": ("construction", "soft_cost_pct"),
    "as.build_share_pct": ("construction", "build_share_pct"),
    "as.ltc_pct": ("financing", "loan_to_cost_pct"),
    "as.rate_pct": ("financing", "rate_pct"),
    "as.points_pct": ("financing", "points_pct"),
    "as.draw_count": ("financing", "draw_count"),
    "as.draw_fee": ("financing", "draw_fee"),
    "as.hold_months": ("holding", "hold_months"),
    "as.tax_rate_pct": ("holding", "property_tax_rate_pct"),
    "as.insurance_pct": ("holding", "insurance_pct_of_hard_cost_per_year"),
    "as.commission_pct": ("selling", "commission_pct"),
    "as.sell_closing_pct": ("selling", "closing_pct"),
    "as.target_margin_pct": ("target", "margin_pct"),
    "as.min_home_sqft": ("sizing", "min_home_sqft"),
    "as.max_home_sqft": ("sizing", "max_home_sqft"),
    "as.min_comps": ("arv", "min_comps"),
    "as.min_comp_sqft": ("arv", "min_comp_sqft"),
    "as.new_build_premium_pct": ("arv", "new_build_premium_pct"),
}
RULE_FIELDS = ("coverage_pct", "stories", "living_share_pct")


def test_every_assumption_equals_the_dallas_pack(values: Any) -> None:
    sheet = values["Assumptions"]
    rows = key_rows(sheet)
    costs = get_pack("dallas").cost_assumptions
    expected: dict[str, Decimal] = {}
    for key, (group, field) in PACK_PATHS.items():
        expected[key] = Decimal(str(getattr(getattr(costs, group), field)))
    for zoning in ("R-7.5(A)", "default"):
        rule = costs.sizing.default if zoning == "default" else costs.sizing.rules[zoning]
        for field in RULE_FIELDS:
            expected[f"as.rule.{zoning}.{field}"] = Decimal(str(getattr(rule, field)))

    assert set(rows) == set(expected)
    for key, want in expected.items():
        assert number(sheet.cell(rows[key], VALUE_COLUMN).value) == want, key


# --- structure of the scenario sheets ------------------------------------------------------


def input_keys(keys: dict[str, int]) -> set[str]:
    return {key for key in keys if key.startswith("in.")}


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_inputs_are_values_and_every_other_key_row_is_a_formula(
    formulas: Any, name: str
) -> None:
    sheet = formulas[name]
    rows = key_rows(sheet)
    inputs = input_keys(rows)
    assert {"in.price", "in.lot_sqft", "in.zoning", "in.is_vacant", "in.existing_sqft"} <= inputs
    assert "in.comp.1.price" in inputs
    assert "in.comp.1.area" in inputs

    for key, row in rows.items():
        cell = sheet.cell(row, VALUE_COLUMN).value
        is_formula = isinstance(cell, str) and cell.startswith("=")
        if key in inputs:
            assert not is_formula or cell in BOOLEAN_INPUT_FORMULAS, key
        else:
            assert is_formula, f"{name} {key} is not a formula: {cell!r}"


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_formulas_reference_assumptions_not_retyped_constants(
    formulas: Any, name: str
) -> None:
    sheet = formulas[name]
    rows = key_rows(sheet)
    for key in ("cost.hard", "cost.contingency", "cost.soft", "fin.loan", "hold.tax"):
        assert "Assumptions!" in sheet.cell(rows[key], VALUE_COLUMN).value, key


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_key_cell_is_an_error_and_only_documented_cells_are_blank(
    values: Any, name: str
) -> None:
    sheet = values[name]
    rows = key_rows(sheet)
    # Blank by design: S2/S3 have no existing area, S3 has no viable offer and so no headroom.
    blank_by_design = {"in.existing_sqft"}
    if name == "S3":
        blank_by_design |= {"max.headroom", "max.profit_at_max"}
    for key, row in rows.items():
        cell = sheet.cell(row, VALUE_COLUMN).value
        if cell is None:
            assert key in blank_by_design, f"{name} {key} is blank"
        if isinstance(cell, str):
            assert not cell.startswith("#"), f"{name} {key} is an error: {cell}"


def test_the_at_max_column_repeats_the_keys_with_an_atmax_prefix(formulas: Any) -> None:
    for name in ("S1", "S2"):
        sheet = formulas[name]
        at_max = key_rows(sheet, AT_MAX_KEY_COLUMN)
        assert "atmax.tot.profit" in at_max
        for key, row in at_max.items():
            assert key.startswith("atmax.")
            assert sheet.cell(row, AT_MAX_VALUE_COLUMN).value.startswith("=")


# --- hand-computed expected values, to the cent ------------------------------

MONEY: dict[str, tuple[str, str, str]] = {
    "sizing.uncapped": ("3168.00", "5016.33", "2480.00"),
    "sizing.buildable_sqft": ("3168", "3500", "2480"),
    "arv.arv": ("1420799.85", "1489400.50", "678417.76"),
    "cost.acq_closing": ("4200.00", "3000.00", "4950.00"),
    "cost.demolition": ("11700.00", "0.00", "14500.00"),
    "cost.hard": ("601920.00", "665000.00", "471200.00"),
    "cost.contingency": ("30096.00", "33250.00", "23560.00"),
    "cost.soft": ("72230.40", "79800.00", "56544.00"),
    "fin.financeable": ("1135946.40", "1078050.00", "1060804.00"),
    "fin.loan": ("908757.12", "862440.00", "848643.20"),
    "fin.interest_front": ("30235.82", "22788.00", "33962.64"),
    "fin.interest_progressive": ("24648.62", "27231.75", "19295.64"),
    "fin.points": ("18175.14", "17248.80", "16972.86"),
    "fin.total": ("74059.58", "68268.55", "71231.14"),
    "hold.tax": ("7014.14", "5010.10", "8266.66"),
    "hold.insurance": ("6771.60", "7481.25", "5301.00"),
    "sell.total": ("85247.99", "89364.04", "40705.07"),
    "tot.total_cost": ("1313239.71", "1251173.94", "1191257.87"),
    "tot.profit": ("107560.14", "238226.56", "-512840.11"),
    "tot.cash_invested": ("319234.60", "299369.90", "301909.60"),
}
RATIOS: dict[str, tuple[str, str, str]] = {
    "arv.median_psf": ("448.4848", "425.5430", "273.55555"),
    "tot.margin": ("0.0757", "0.1599", "-0.7559"),
    "tot.roi": ("0.3369", "0.7958", "-1.6987"),
    "tot.annualized": ("0.4492", "1.0610", "-2.2649"),
    "max.price_coeff": ("1.102700325", "1.102700325", "1.102700325"),
}
FIXED_PART = ("850105.5804", "920363.8400", "645421.2140")
MAX_OFFER = ("324271.50", "313436.54", None)  # S3 has no viable offer
HEADROOM = ("-95728.50", "13436.54", None)


def scenario_value(values: Any, name: str, key: str) -> Any:
    sheet = values[name]
    return sheet.cell(key_rows(sheet)[key], VALUE_COLUMN).value


@pytest.mark.parametrize("index", [0, 1, 2], ids=SCENARIOS)
def test_money_lines_match_the_formula_section_to_the_cent(values: Any, index: int) -> None:
    name = SCENARIOS[index]
    for key, expected in MONEY.items():
        got = number(scenario_value(values, name, key))
        assert abs(got - Decimal(expected[index])) <= CENT, f"{name} {key}: {got}"


@pytest.mark.parametrize("index", [0, 1, 2], ids=SCENARIOS)
def test_ratios_match_the_formula_section(values: Any, index: int) -> None:
    name = SCENARIOS[index]
    for key, expected in RATIOS.items():
        got = number(scenario_value(values, name, key))
        assert abs(got - Decimal(expected[index])) <= EXACT_RATIO, f"{name} {key}: {got}"


@pytest.mark.parametrize("index", [0, 1, 2], ids=SCENARIOS)
def test_the_maximum_offer_lines_match_the_formula_section(values: Any, index: int) -> None:
    name = SCENARIOS[index]
    fixed = number(scenario_value(values, name, "max.fixed_part"))
    assert abs(fixed - Decimal(FIXED_PART[index])) <= Decimal("0.0001")

    offer = scenario_value(values, name, "max.max_offer")
    headroom = scenario_value(values, name, "max.headroom")
    if MAX_OFFER[index] is None:
        assert offer == "none"
        assert headroom in (None, "")
        assert number(scenario_value(values, name, "max.numerator")) < 0
    else:
        assert abs(number(offer) - Decimal(str(MAX_OFFER[index]))) <= CENT
        assert abs(number(headroom) - Decimal(str(HEADROOM[index]))) <= CENT


@pytest.mark.parametrize(("name", "profit"), [("S1", "213119.98"), ("S2", "223410.09")])
def test_profit_at_the_maximum_offer_is_the_target_within_five_cents(
    values: Any, name: str, profit: str
) -> None:
    sheet = values[name]
    at_max = number(
        sheet.cell(
            key_rows(sheet, AT_MAX_KEY_COLUMN)["atmax.tot.profit"], AT_MAX_VALUE_COLUMN
        ).value
    )
    target = number(scenario_value(values, name, "max.target_profit"))

    assert abs(at_max - Decimal(profit)) <= CENT
    assert abs(at_max - target) <= Decimal("0.05")


def test_the_summary_shows_each_scenario_outcome(values: Any) -> None:
    rows = list(values["Summary"].iter_rows(min_row=2, max_row=4, values_only=True))

    assert [row[0] for row in rows] == SCENARIOS
    assert [number(row[1]) for row in rows] == [420000, 300000, 495000]
    for row, profit in zip(rows, ("107560.14", "238226.56", "-512840.11"), strict=True):
        assert abs(number(row[4]) - Decimal(profit)) <= CENT, row[0]
    assert rows[2][8] == "none"


# --- sensitivity grid ----------------------------------------------------------------------

GRID_SPOT_CHECKS = {
    (0, 0, 9): ("107560.14", "0.0757"),
    (10, -10, 6): ("337940.43", "0.2162"),
    (-10, 20, 12): ("-201522.39", "-0.1576"),
    (-5, 0, 6): ("63672.60", "0.0472"),
    (5, 20, 12): ("-1189.62", "-0.0008"),
}


def test_the_sensitivity_grid_has_sixty_rows_in_the_documented_order(values: Any) -> None:
    rows = list(values["Sensitivity"].iter_rows(min_row=2, max_row=61, max_col=9, values_only=True))
    grid = get_pack("dallas").cost_assumptions.sensitivity
    expected = [
        (d, e, h)
        for d in grid.arv_delta_pct
        for e in grid.hard_cost_delta_pct
        for h in grid.hold_months
    ]

    assert [tuple(row[:3]) for row in rows] == expected
    header = [cell.value for cell in values["Sensitivity"][1][:9]]
    assert header == [
        "arv_delta_pct",
        "hard_cost_delta_pct",
        "hold_months",
        "arv",
        "hard_cost",
        "total_cost",
        "profit",
        "margin",
        "roi",
    ]
    assert values["Sensitivity"].max_row == 61
    assert all(cell is not None for row in rows for cell in row)


def test_the_grid_spot_checks_and_its_centre_equal_the_base_case(values: Any) -> None:
    grid = {
        tuple(row[:3]): row
        for row in values["Sensitivity"].iter_rows(
            min_row=2, max_row=61, max_col=9, values_only=True
        )
    }
    for point, (profit, margin) in GRID_SPOT_CHECKS.items():
        assert abs(number(grid[point][6]) - Decimal(profit)) <= CENT, point
        assert abs(number(grid[point][7]) - Decimal(margin)) <= RATIO, point

    base_profit = number(scenario_value(values, "S1", "tot.profit"))
    base_margin = number(scenario_value(values, "S1", "tot.margin"))
    assert number(grid[(0, 0, 9)][6]) == base_profit
    assert number(grid[(0, 0, 9)][7]) == base_margin


def test_the_sensitivity_formulas_are_live(formulas: Any) -> None:
    rows = formulas["Sensitivity"].iter_rows(min_row=2, max_row=61, min_col=4, max_col=9)
    for row in rows:
        assert all(isinstance(cell.value, str) and cell.value.startswith("=") for cell in row)


# --- the engine against the workbook ---------------------------------------------------------
#
# The scenario inputs are read from the workbook's own input rows, so they are never typed twice.
# Every key row on a scenario sheet must be compared; a key the engine cannot yet be compared on
# fails the test rather than being skipped.

AS_OF = date(2026, 10, 2)
ENGINE_CENT = Decimal("0.005")  # figures that are rounded to cents compare to the cent
UNROUNDED = Decimal("0.000001")  # figures the formulas leave unrounded compare to 1e-6
AT_MAX_PREFIX = "atmax."


def comparison_inputs(values: Any, name: str) -> ProformaInputs:
    sheet = values[name]
    rows = key_rows(sheet)

    def cell(key: str) -> Any:
        return sheet.cell(rows[key], VALUE_COLUMN).value

    def decimal_or_none(key: str) -> Decimal | None:
        value = cell(key)
        return None if value is None else number(value)

    comps = []
    index = 1
    while f"in.comp.{index}.price" in rows:
        comps.append(
            Comp(
                address=f"{index} Comp St",
                price=number(cell(f"in.comp.{index}.price")),
                living_area_sqft=int(cell(f"in.comp.{index}.area")),
                distance_miles=None,
                year_built=None,
            )
        )
        index += 1
    zoning = cell("in.zoning")
    return ProformaInputs(
        price=number(cell("in.price")),
        lot_sqft=number(cell("in.lot_sqft")),
        lot_source="parcel",
        zoning=zoning,
        zoning_values_seen=(zoning,),
        is_vacant=bool(cell("in.is_vacant")),
        is_gis_group=False,
        existing_living_sqft=decimal_or_none("in.existing_sqft"),
        as_of=AS_OF,
        estimate=EstimateInput(
            fetched_on=AS_OF, outcome="ok", price=Decimal(1), comps=tuple(comps)
        ),
    )


def run_engine(case: ProformaInputs) -> ProformaResult:
    pack = get_pack("dallas")
    return build_proforma(
        case, pack.cost_assumptions, estimate_ttl_days=pack.sourcing.estimates.ttl_days
    )


Kind = str  # "money" | "ratio" | "exact"


def engine_lines(result: ProformaResult) -> dict[str, tuple[Kind, Any]]:
    """The engine's value for each workbook key (without the at-max prefix)."""
    sizing, arv, costs = result.sizing, result.arv, result.costs
    financing, holding, selling, totals = (
        result.financing,
        result.holding,
        result.selling,
        result.totals,
    )
    assert sizing and arv and costs and financing and holding and selling and totals
    return {
        "sizing.rule_coverage_pct": ("exact", sizing.coverage_pct),
        "sizing.rule_stories": ("exact", sizing.stories),
        "sizing.rule_living_share_pct": ("exact", sizing.living_share_pct),
        "sizing.footprint": ("exact", sizing.footprint_sqft),
        "sizing.gross": ("exact", sizing.gross_sqft),
        "sizing.uncapped": ("exact", sizing.uncapped_sqft),
        "sizing.buildable_sqft": ("exact", sizing.buildable_sqft),
        "arv.comp_count_used": ("exact", arv.comp_count_used),
        "arv.median_psf": ("exact", arv.median_psf),
        "arv.arv": ("money", arv.arv),
        "cost.acq_closing": ("money", costs.acquisition_closing),
        "cost.demolition": ("money", costs.demolition),
        "cost.hard": ("money", costs.hard_cost),
        "cost.contingency": ("money", costs.contingency),
        "cost.soft": ("money", costs.soft_costs),
        "fin.months_construction": ("exact", financing.months.construction),
        "fin.months_sale": ("exact", financing.months.sale),
        "fin.financeable": ("money", financing.financeable_cost),
        "fin.loan": ("money", financing.loan_amount),
        "fin.interest_front": ("money", financing.interest_front),
        "fin.interest_progressive": ("money", financing.interest_progressive),
        "fin.interest": ("money", financing.interest),
        "fin.points": ("money", financing.points),
        "fin.draw_fees": ("money", financing.draw_fees),
        "fin.total": ("money", financing.total),
        "hold.tax": ("money", holding.property_tax),
        "hold.insurance": ("money", holding.insurance),
        "hold.total": ("money", holding.total),
        "sell.commission": ("money", selling.commission),
        "sell.closing": ("money", selling.closing),
        "sell.total": ("money", selling.total),
        "tot.total_cost": ("money", totals.total_cost),
        "tot.profit": ("money", totals.profit),
        "tot.margin": ("ratio", totals.margin),
        "tot.cash_invested": ("money", totals.cash_invested),
        "tot.roi": ("ratio", totals.roi),
        "tot.annualized": ("ratio", totals.annualized_return),
    }


def max_offer_lines(result: ProformaResult) -> dict[str, tuple[Kind, Any]]:
    offer, arv = result.max_offer, result.arv
    assert offer and arv and arv.arv is not None
    return {
        "max.fixed_part": ("exact", offer.fixed_part),
        "max.price_coeff": ("exact", offer.price_coefficient),
        "max.numerator": ("exact", arv.arv * (1 - offer.target_margin) - offer.fixed_part),
        "max.target_profit": ("money", round_money(arv.arv * offer.target_margin)),
    }


def assert_matches(key: str, kind: Kind, engine: Any, sheet_value: Any) -> None:
    where = f"{key}: engine {engine!r} vs workbook {sheet_value!r}"
    if sheet_value is None:
        assert engine is None, where
        return
    assert engine is not None, where
    wanted = number(sheet_value)
    tolerance = {"money": ENGINE_CENT, "ratio": EXACT_RATIO, "exact": UNROUNDED}[kind]
    assert abs(Decimal(engine) - wanted) <= tolerance, where


@pytest.fixture(scope="module")
def engine_results(values: Any) -> dict[str, ProformaResult]:
    return {name: run_engine(comparison_inputs(values, name)) for name in SCENARIOS}


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_key_row_of_a_scenario_matches_the_engine(
    values: Any, engine_results: dict[str, ProformaResult], name: str
) -> None:
    sheet = values[name]
    rows = key_rows(sheet)
    result = engine_results[name]
    assert result.status == "computed"
    known = {**engine_lines(result), **max_offer_lines(result)}
    compared: set[str] = set()

    for key, row in rows.items():
        if key.startswith("in."):
            continue
        if key in ("max.max_offer", "max.headroom", "max.profit_at_max"):
            continue  # compared below, where the workbook's "none" is mapped to no offer
        assert key in known, f"{name}: no engine value is compared to {key}"
        assert_matches(f"{name} {key}", known[key][0], known[key][1], sheet.cell(row, 2).value)
        compared.add(key)

    assert len(compared) > 40


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_maximum_offer_agrees_and_none_means_no_offer(
    values: Any, engine_results: dict[str, ProformaResult], name: str
) -> None:
    offer = engine_results[name].max_offer
    assert offer is not None
    workbook_offer = scenario_value(values, name, "max.max_offer")
    workbook_headroom = scenario_value(values, name, "max.headroom")

    if workbook_offer == "none":
        assert offer.max_offer is None
        assert offer.headroom_vs_offer is None
        assert "no_viable_offer" in engine_results[name].flags
        assert workbook_headroom in (None, "")
    else:
        assert offer.max_offer is not None and offer.headroom_vs_offer is not None
        assert abs(offer.max_offer - number(workbook_offer)) <= ENGINE_CENT
        assert abs(offer.headroom_vs_offer - number(workbook_headroom)) <= ENGINE_CENT
        assert "no_viable_offer" not in engine_results[name].flags


@pytest.mark.parametrize("name", ["S1", "S2"])
def test_the_engine_at_the_workbook_maximum_offer_matches_the_at_max_column(
    values: Any, engine_results: dict[str, ProformaResult], name: str
) -> None:
    sheet = values[name]
    at_max_rows = key_rows(sheet, AT_MAX_KEY_COLUMN)
    offer = scenario_value(values, name, "max.max_offer")
    case = comparison_inputs(values, name).model_copy(update={"price": number(offer)})
    at_max = engine_lines(run_engine(case))

    assert len(at_max_rows) > 20
    for key, row in at_max_rows.items():
        bare = key.removeprefix(AT_MAX_PREFIX)
        assert bare in at_max, f"{name}: no engine value is compared to {key}"
        kind, engine = at_max[bare]
        assert_matches(f"{name} {key}", kind, engine, sheet.cell(row, AT_MAX_VALUE_COLUMN).value)

    # The engine's own maximum offer is the workbook's, so it reaches the same profit.
    own = engine_results[name].max_offer
    assert own is not None and own.max_offer == number(offer)


def test_the_sensitivity_grid_matches_the_workbook_cell_by_cell(
    values: Any, engine_results: dict[str, ProformaResult]
) -> None:
    table = engine_results["S1"].sensitivity
    assert table is not None
    rows = list(values["Sensitivity"].iter_rows(min_row=2, max_row=61, max_col=9, values_only=True))
    assert len(table.cells) == len(rows) == 60

    for cell, row in zip(table.cells, rows, strict=True):
        where = (cell.arv_delta_pct, cell.hard_cost_delta_pct, cell.hold_months)
        assert where == tuple(number(value) for value in row[:3])
        for field, kind, value in [
            ("arv", "money", row[3]),
            ("hard_cost", "money", row[4]),
            ("total_cost", "money", row[5]),
            ("profit", "money", row[6]),
            ("margin", "ratio", row[7]),
            ("roi", "ratio", row[8]),
        ]:
            assert_matches(f"{where} {field}", kind, getattr(cell, field), value)


def test_the_centre_of_the_grid_is_the_base_result(
    engine_results: dict[str, ProformaResult],
) -> None:
    result = engine_results["S1"]
    assert result.sensitivity and result.totals and result.arv
    centre = next(
        c
        for c in result.sensitivity.cells
        if (c.arv_delta_pct, c.hard_cost_delta_pct) == (0, 0) and c.hold_months == 9
    )

    assert (centre.arv, centre.total_cost, centre.profit, centre.margin, centre.roi) == (
        result.arv.arv,
        result.totals.total_cost,
        result.totals.profit,
        result.totals.margin,
        result.totals.roi,
    )

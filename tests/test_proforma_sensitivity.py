from decimal import Decimal

from feasibility.markets.loader import get_pack
from feasibility.proforma.chain import cost_chain
from feasibility.proforma.model import SensitivityCell, SensitivityTable
from feasibility.proforma.sensitivity import sensitivity_grid

ASSUMPTIONS = get_pack("dallas").cost_assumptions
HOLD = ASSUMPTIONS.holding.hold_months
BASE = cost_chain(
    Decimal("420000"),
    Decimal("11700.00"),
    Decimal("601920.00"),
    Decimal("1420799.85"),
    HOLD,
    ASSUMPTIONS,
)


def cell(table: SensitivityTable, arv_delta: int, hard_delta: int, hold: int) -> SensitivityCell:
    matches = [
        c
        for c in table.cells
        if (c.arv_delta_pct, c.hard_cost_delta_pct, c.hold_months)
        == (Decimal(arv_delta), Decimal(hard_delta), Decimal(hold))
    ]
    assert len(matches) == 1
    return matches[0]


def test_the_grid_has_sixty_cells_in_the_documented_order() -> None:
    table = sensitivity_grid(BASE, ASSUMPTIONS)
    grid = ASSUMPTIONS.sensitivity

    assert len(table.cells) == 60
    assert [(c.arv_delta_pct, c.hard_cost_delta_pct, c.hold_months) for c in table.cells] == [
        (d, e, h)
        for d in grid.arv_delta_pct
        for e in grid.hard_cost_delta_pct
        for h in grid.hold_months
    ]
    assert table.axes.arv_delta_pct == tuple(grid.arv_delta_pct)
    assert table.axes.hard_cost_delta_pct == tuple(grid.hard_cost_delta_pct)
    assert table.axes.hold_months == tuple(grid.hold_months)


def test_the_centre_cell_equals_the_base_case() -> None:
    centre = cell(sensitivity_grid(BASE, ASSUMPTIONS), 0, 0, 9)

    assert BASE.totals is not None
    assert centre.arv == BASE.arv
    assert centre.hard_cost == BASE.costs.hard_cost
    assert centre.total_cost == BASE.totals.total_cost
    assert centre.profit == BASE.totals.profit
    assert centre.margin == BASE.totals.margin
    assert centre.roi == BASE.totals.roi


def test_spot_checks_from_the_formula_section() -> None:
    table = sensitivity_grid(BASE, ASSUMPTIONS)

    for point, profit, margin in [
        ((10, -10, 6), "337940.43", "0.2162"),
        ((-10, 20, 12), "-201522.39", "-0.1576"),
        ((-5, 0, 6), "63672.60", "0.0472"),
        ((5, 20, 12), "-1189.62", "-0.0008"),
    ]:
        found = cell(table, *point)
        assert found.profit == Decimal(profit), point
        assert found.margin == Decimal(margin), point


def test_a_cell_reprices_the_arv_and_the_hard_cost_before_costing() -> None:
    found = cell(sensitivity_grid(BASE, ASSUMPTIONS), -10, -10, 6)

    assert found.arv == Decimal("1278719.87")  # 1,420,799.85 x 0.90 rounded to cents
    assert found.hard_cost == Decimal("541728.00")
    assert found.total_cost == Decimal("1207889.81")


def test_the_grid_follows_the_pack_axes() -> None:
    grid = ASSUMPTIONS.sensitivity.model_copy(
        update={
            "arv_delta_pct": [Decimal(0), Decimal(2)],
            "hard_cost_delta_pct": [Decimal(0)],
            "hold_months": [Decimal(9), Decimal(10)],
        }
    )
    assumptions = ASSUMPTIONS.model_copy(update={"sensitivity": grid})

    assert len(sensitivity_grid(BASE, assumptions).cells) == 4

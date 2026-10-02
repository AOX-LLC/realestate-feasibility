"""The sensitivity grid: the base project re-costed with the ARV, hard cost and hold changed."""

from decimal import Decimal

from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.chain import PERCENT, cost_chain
from feasibility.proforma.model import (
    CostChain,
    SensitivityAxes,
    SensitivityCell,
    SensitivityTable,
)
from feasibility.proforma.money import round_money


def sensitivity_grid(base: CostChain, assumptions: CostAssumptions) -> SensitivityTable:
    """One cell per (ARV change, hard-cost change, hold), ARV outermost and hold innermost.

    Price and demolition stay as in the base; the changed hard cost drives contingency, soft
    costs and insurance, and the changed ARV drives the selling costs.
    """
    if base.arv is None:
        raise ValueError("the sensitivity grid needs a chain with an ARV")
    axes = assumptions.sensitivity
    cells = [
        _cell(base, base.arv, arv_delta, hard_delta, hold, assumptions)
        for arv_delta in axes.arv_delta_pct
        for hard_delta in axes.hard_cost_delta_pct
        for hold in axes.hold_months
    ]
    return SensitivityTable(
        axes=SensitivityAxes(
            arv_delta_pct=tuple(axes.arv_delta_pct),
            hard_cost_delta_pct=tuple(axes.hard_cost_delta_pct),
            hold_months=tuple(axes.hold_months),
        ),
        cells=tuple(cells),
    )


def _cell(
    base: CostChain,
    base_arv: Decimal,
    arv_delta: Decimal,
    hard_delta: Decimal,
    hold: Decimal,
    assumptions: CostAssumptions,
) -> SensitivityCell:
    arv = round_money(base_arv * (1 + arv_delta / PERCENT))
    hard_cost = round_money(base.costs.hard_cost * (1 + hard_delta / PERCENT))
    chain = cost_chain(
        base.costs.offer_price, base.costs.demolition, hard_cost, arv, hold, assumptions
    )
    if chain.totals is None:
        raise ValueError("a chain with an ARV has totals")
    return SensitivityCell(
        arv_delta_pct=arv_delta,
        hard_cost_delta_pct=hard_delta,
        hold_months=hold,
        arv=arv,
        hard_cost=hard_cost,
        total_cost=chain.totals.total_cost,
        profit=chain.totals.profit,
        margin=chain.totals.margin,
        roi=chain.totals.roi,
    )

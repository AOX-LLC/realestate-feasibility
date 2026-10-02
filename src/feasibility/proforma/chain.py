"""Lines 9-28 of the pro-forma formulas as one pure function.

Sensitivity and the maximum offer call it again with other prices, hard costs, ARVs and holds,
so there is exactly one copy of the arithmetic. Every money line is rounded to cents where it is
defined and later lines use the rounded value. Interest and carrying costs multiply first and
divide once, so a figure that is exactly half a cent is rounded as such.
"""

from decimal import Decimal

from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.model import (
    CostChain,
    Costs,
    FinancingMonths,
    FinancingResult,
    HoldingResult,
    SellingResult,
    Totals,
)
from feasibility.proforma.money import round_money, round_ratio

PERCENT = Decimal(100)
MONTHS_PER_YEAR = Decimal(12)
# loan-to-cost % x rate % x months, over 100 x 100 x 12
INTEREST_DIVISOR = PERCENT * PERCENT * MONTHS_PER_YEAR
CARRY_DIVISOR = PERCENT * MONTHS_PER_YEAR


def cost_chain(
    price: Decimal,
    demolition: Decimal,
    hard_cost: Decimal,
    arv: Decimal | None,
    hold_months: Decimal,
    assumptions: CostAssumptions,
) -> CostChain:
    """Cost, finance, hold and (given an ARV) sell one project."""
    costs = Costs(
        offer_price=price,
        acquisition_closing=round_money(price * assumptions.acquisition.closing_pct / PERCENT),
        demolition=demolition,
        hard_cost=hard_cost,
        contingency=round_money(hard_cost * assumptions.construction.contingency_pct / PERCENT),
        soft_costs=round_money(hard_cost * assumptions.construction.soft_cost_pct / PERCENT),
    )
    financing = _financing(costs, hold_months, assumptions)
    holding = _holding(costs, hold_months, assumptions)
    if arv is None:
        return CostChain(
            costs=costs, arv=None, financing=financing, holding=holding, selling=None, totals=None
        )

    selling = _selling(arv, assumptions)
    totals = _totals(costs, financing, holding, selling, arv, hold_months)
    return CostChain(
        costs=costs, arv=arv, financing=financing, holding=holding, selling=selling, totals=totals
    )


def _financing(costs: Costs, hold_months: Decimal, assumptions: CostAssumptions) -> FinancingResult:
    terms = assumptions.financing
    construction = hold_months * assumptions.construction.build_share_pct / PERCENT
    sale = hold_months - construction
    price, demolition = costs.offer_price, costs.demolition
    build = costs.hard_cost + costs.contingency
    financeable = price + demolition + build + costs.soft_costs
    loan = round_money(financeable * terms.loan_to_cost_pct / PERCENT)

    interest_rate = terms.loan_to_cost_pct * terms.rate_pct
    front_balance = price + demolition + costs.soft_costs
    interest_front = round_money(front_balance * interest_rate * hold_months / INTEREST_DIVISOR)
    interest_progressive = round_money(
        build * interest_rate * (construction / 2 + sale) / INTEREST_DIVISOR
    )
    interest = interest_front + interest_progressive
    points = round_money(loan * terms.points_pct / PERCENT)
    draw_fees = round_money(terms.draw_count * terms.draw_fee)
    return FinancingResult(
        months=FinancingMonths(hold=hold_months, construction=construction, sale=sale),
        financeable_cost=financeable,
        loan_amount=loan,
        interest_front=interest_front,
        interest_progressive=interest_progressive,
        interest=interest,
        points=points,
        draw_fees=draw_fees,
        total=interest + points + draw_fees,
    )


def _holding(costs: Costs, hold_months: Decimal, assumptions: CostAssumptions) -> HoldingResult:
    terms = assumptions.holding
    tax = round_money(costs.offer_price * terms.property_tax_rate_pct * hold_months / CARRY_DIVISOR)
    insurance = round_money(
        costs.hard_cost * terms.insurance_pct_of_hard_cost_per_year * hold_months / CARRY_DIVISOR
    )
    return HoldingResult(property_tax=tax, insurance=insurance, total=tax + insurance)


def _selling(arv: Decimal, assumptions: CostAssumptions) -> SellingResult:
    commission = round_money(arv * assumptions.selling.commission_pct / PERCENT)
    closing = round_money(arv * assumptions.selling.closing_pct / PERCENT)
    return SellingResult(commission=commission, closing=closing, total=commission + closing)


def _totals(
    costs: Costs,
    financing: FinancingResult,
    holding: HoldingResult,
    selling: SellingResult,
    arv: Decimal,
    hold_months: Decimal,
) -> Totals:
    total_cost = (
        costs.offer_price
        + costs.acquisition_closing
        + costs.demolition
        + costs.hard_cost
        + costs.contingency
        + costs.soft_costs
        + financing.total
        + holding.total
        + selling.total
    )
    profit = arv - total_cost
    cash_invested = total_cost - selling.total - financing.loan_amount
    has_cash = cash_invested > 0
    return Totals(
        total_cost=total_cost,
        profit=profit,
        margin=round_ratio(profit / arv),
        cash_invested=cash_invested,
        roi=round_ratio(profit / cash_invested) if has_cash else None,
        annualized_return=(
            round_ratio(profit * MONTHS_PER_YEAR / (cash_invested * hold_months))
            if has_cash
            else None
        ),
    )

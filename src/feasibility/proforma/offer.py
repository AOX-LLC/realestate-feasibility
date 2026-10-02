"""The offer that reaches the target margin, from the closed form (the model is linear in price).

Profit falls by a fixed amount for every dollar of price, so the offer has a closed form:
total cost = A + B x price, and the target needs ARV - total cost >= ARV x margin.
"""

from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.chain import CARRY_DIVISOR, INTEREST_DIVISOR, PERCENT
from feasibility.proforma.model import CostChain, MaxOffer
from feasibility.proforma.money import round_money_down


def max_offer(chain: CostChain, assumptions: CostAssumptions) -> MaxOffer:
    """The offer that earns the target margin, to within a few cents, given a chain with an ARV.

    The closed form ignores the rounding of each cost line, so the profit at the returned offer can
    differ from the target by up to two cents either way.

    The parts of the chain that do not move with the offer price (demolition, construction,
    insurance, selling) are read from `chain`; the rest are rebuilt as a coefficient on price.
    """
    if chain.arv is None or chain.selling is None:
        raise ValueError("the maximum offer needs a chain with an ARV")
    arv = chain.arv
    costs, financing = chain.costs, chain.financing
    terms = assumptions.financing
    months = financing.months

    cost_without_price = costs.demolition + costs.hard_cost + costs.contingency + costs.soft_costs
    ltc_times_rate = (
        terms.loan_to_cost_pct * terms.rate_pct
    )  # in %-squared; INTEREST_DIVISOR undoes it
    interest_months = (costs.demolition + costs.soft_costs) * months.hold + (
        costs.hard_cost + costs.contingency
    ) * (months.construction / 2 + months.sale)
    fixed_part = (
        cost_without_price
        + cost_without_price * terms.loan_to_cost_pct * terms.points_pct / (PERCENT * PERCENT)
        + ltc_times_rate * interest_months / INTEREST_DIVISOR
        + financing.draw_fees
        + chain.holding.insurance
        + chain.selling.total
    )
    price_coefficient = (
        1
        + assumptions.acquisition.closing_pct / PERCENT
        + terms.loan_to_cost_pct * terms.points_pct / (PERCENT * PERCENT)
        + ltc_times_rate * months.hold / INTEREST_DIVISOR
        + assumptions.holding.property_tax_rate_pct * months.hold / CARRY_DIVISOR
    )

    target_margin = assumptions.target.margin_pct / PERCENT
    numerator = arv * (1 - target_margin) - fixed_part
    offer = None if numerator < 0 else round_money_down(numerator / price_coefficient)
    return MaxOffer(
        target_margin=target_margin,
        fixed_part=fixed_part,
        price_coefficient=price_coefficient,
        max_offer=offer,
        headroom_vs_offer=None if offer is None else offer - costs.offer_price,
    )

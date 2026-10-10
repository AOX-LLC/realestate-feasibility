"""The pro-forma for one candidate: size, price, cost, finance, sell, and say what it can pay.

Pure: the inputs and the pack's cost assumptions go in, a `ProformaResult` comes out. Nothing is
read from or written to a database, and nothing is invented. A candidate with no usable value
estimate has no ARV and gets a `no_arv` status with the costs that do not need one.
"""

from dataclasses import dataclass
from decimal import Decimal

from feasibility.markets.schema import CostAssumptions, normalise_zoning
from feasibility.proforma.arv import ArvUnavailable, price_arv
from feasibility.proforma.assumptions_v1 import CostAssumptionsV1
from feasibility.proforma.chain import cost_chain
from feasibility.proforma.model import (
    ArvDetail,
    CompLine,
    EstimateInput,
    ExistingSqftSource,
    ProformaInputs,
    ProformaResult,
    Site,
    SizingResult,
    Status,
)
from feasibility.proforma.money import round_money
from feasibility.proforma.offer import max_offer
from feasibility.proforma.sensitivity import sensitivity_grid
from feasibility.proforma.sizing import buildable_size

NO_ESTIMATE_YET = "no_estimate_yet"
ESTIMATE_UNAVAILABLE = "estimate_unavailable"
ESTIMATE_EXPIRED = "estimate_expired"
LOT_SIZE_MISSING = "lot_size_missing"


@dataclass(frozen=True)
class _ArvOutcome:
    detail: ArvDetail | None
    arv: Decimal | None
    reason: str | None  # why there is no ARV
    stale: bool


def build_proforma(
    inputs: ProformaInputs, assumptions: CostAssumptions, *, estimate_ttl_days: int
) -> ProformaResult:
    """Run the pro-forma. `estimate_ttl_days` is how young an estimate must be to count as fresh."""
    flags: list[str] = []
    if inputs.is_gis_group:
        flags.append("gis_group")
    zoning = _single_zoning(inputs)
    if zoning is None and _zonings_disagree(inputs):
        flags.append("zoning_mixed")

    if inputs.lot_sqft is None or inputs.lot_sqft <= 0:
        site = _site(inputs, rule_used=None, existing_source=_existing_source(inputs))
        return _empty(assumptions, "unsizable", LOT_SIZE_MISSING, flags, site)

    choice = buildable_size(inputs.lot_sqft, zoning, assumptions.sizing)
    flags.extend(choice.flags)
    demolition, existing_source, existing_flag = _demolition(inputs, assumptions)
    if existing_flag:
        flags.append(existing_flag)
    hard_cost = round_money(
        choice.sizing.buildable_sqft * assumptions.construction.hard_cost_per_sqft
    )

    outcome = _resolve_arv(inputs, assumptions, choice.sizing, estimate_ttl_days)
    if outcome.arv is not None and outcome.stale:
        flags.append("estimate_stale")
    chain = cost_chain(
        inputs.price,
        demolition,
        hard_cost,
        outcome.arv,
        assumptions.holding.hold_months,
        assumptions,
    )
    site = _site(inputs, rule_used=choice.rule_used, existing_source=existing_source)
    status: Status = "computed" if outcome.arv is not None else "no_arv"

    offer = table = None
    if chain.totals is not None:
        if chain.totals.profit < -chain.totals.cash_invested:
            flags.append("loss_exceeds_equity")
        offer = max_offer(chain, assumptions)
        if offer.max_offer is None:
            flags.append("no_viable_offer")
        table = sensitivity_grid(chain, assumptions)

    return ProformaResult(
        status=status,
        reason=outcome.reason,
        flags=tuple(flags),
        assumptions=frozen_assumptions(assumptions),
        site=site,
        sizing=choice.sizing,
        arv=outcome.detail,
        costs=chain.costs,
        financing=chain.financing,
        holding=chain.holding,
        selling=chain.selling,
        totals=chain.totals,
        max_offer=offer,
        sensitivity=table,
    )


def frozen_assumptions(assumptions: CostAssumptions) -> CostAssumptionsV1:
    """The pack's assumptions in the shape a stored result keeps them in."""
    return CostAssumptionsV1.model_validate(assumptions.model_dump())


def _empty(
    assumptions: CostAssumptions,
    status: Status,
    reason: str,
    flags: list[str],
    site: Site,
) -> ProformaResult:
    return ProformaResult(
        status=status,
        reason=reason,
        flags=tuple(flags),
        assumptions=frozen_assumptions(assumptions),
        site=site,
        sizing=None,
        arv=None,
        costs=None,
        financing=None,
        holding=None,
        selling=None,
        totals=None,
        max_offer=None,
        sensitivity=None,
    )


def _single_zoning(inputs: ProformaInputs) -> str | None:
    """The zoning to size by: none when the accounts of a group disagree."""
    return None if _zonings_disagree(inputs) else inputs.zoning


def _zonings_disagree(inputs: ProformaInputs) -> bool:
    seen = {normalise_zoning(value) for value in inputs.zoning_values_seen if value.strip()}
    return len(seen) > 1


def _existing_source(inputs: ProformaInputs) -> ExistingSqftSource:
    if inputs.is_vacant:
        return "vacant"
    known = inputs.existing_living_sqft is not None and inputs.existing_living_sqft > 0
    return "parcel" if known else "assumed"


def _demolition(
    inputs: ProformaInputs, assumptions: CostAssumptions
) -> tuple[Decimal, ExistingSqftSource, str | None]:
    source = _existing_source(inputs)
    terms = assumptions.demolition
    if source == "vacant":
        return round_money(Decimal(0)), source, "vacant_lot"
    if source == "assumed" or inputs.existing_living_sqft is None:
        existing = terms.fallback_sqft
        flag = "existing_area_assumed"
    else:
        existing = inputs.existing_living_sqft
        flag = None
    return round_money(terms.flat + terms.per_sqft * existing), source, flag


def _site(
    inputs: ProformaInputs, *, rule_used: str | None, existing_source: ExistingSqftSource
) -> Site:
    return Site(
        lot_sqft=inputs.lot_sqft,
        lot_source=inputs.lot_source,
        zoning=inputs.zoning,
        zoning_values_seen=inputs.zoning_values_seen,
        rule_used=rule_used,
        is_vacant=inputs.is_vacant,
        is_gis_group=inputs.is_gis_group,
        existing_living_sqft=inputs.existing_living_sqft,
        existing_sqft_source=existing_source,
    )


def _resolve_arv(
    inputs: ProformaInputs,
    assumptions: CostAssumptions,
    sizing: SizingResult,
    estimate_ttl_days: int,
) -> _ArvOutcome:
    """Apply the estimate rules: which estimate may price the house, and what it prices it at."""
    estimate = inputs.estimate
    if estimate is None:
        return _ArvOutcome(detail=None, arv=None, reason=NO_ESTIMATE_YET, stale=False)

    rules = assumptions.arv
    age_days = (inputs.as_of - estimate.fetched_on).days
    stale = age_days > estimate_ttl_days
    premium = rules.new_build_premium_pct
    if estimate.outcome == "no_estimate":
        return _unpriced(estimate, premium, stale, ESTIMATE_UNAVAILABLE)
    if age_days > rules.estimate_max_age_days:
        return _unpriced(estimate, premium, stale, ESTIMATE_EXPIRED)

    priced = price_arv(estimate.comps, sizing.buildable_sqft, rules)
    if isinstance(priced, ArvUnavailable):
        detail = _detail(estimate, premium, stale, priced.comps, priced.comp_count_used)
        return _ArvOutcome(detail=detail, arv=None, reason=priced.reason, stale=stale)
    detail = _detail(
        estimate,
        premium,
        stale,
        priced.comps,
        priced.comp_count_used,
        median_psf=priced.median_psf,
        arv=priced.arv,
    )
    return _ArvOutcome(detail=detail, arv=priced.arv, reason=None, stale=stale)


def _unpriced(estimate: EstimateInput, premium: Decimal, stale: bool, reason: str) -> _ArvOutcome:
    detail = _detail(estimate, premium, stale, (), 0)
    return _ArvOutcome(detail=detail, arv=None, reason=reason, stale=stale)


def _detail(
    estimate: EstimateInput,
    premium: Decimal,
    stale: bool,
    comps: tuple[CompLine, ...],
    used: int,
    *,
    median_psf: Decimal | None = None,
    arv: Decimal | None = None,
) -> ArvDetail:
    return ArvDetail(
        estimate_fetched_on=estimate.fetched_on,
        estimate_stale=stale,
        estimate_price=estimate.price,
        comp_count_used=used,
        comp_count_dropped=len(comps) - used,
        comps=comps,
        median_psf=median_psf,
        premium_pct=premium,
        arv=arv,
    )

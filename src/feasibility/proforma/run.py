"""Stage 5 of a sourcing run: a pro-forma for every ranked candidate, in one transaction.

Code only: no network, no spend. The estimates the run bought (or reused) are read from the
database, so a retry of this stage recomputes everything from what is already stored.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import Connection

from feasibility.markets.schema import MarketPack
from feasibility.proforma.engine import build_proforma
from feasibility.proforma.gather import CandidateInputs, gather_inputs
from feasibility.proforma.model import ProformaResult
from feasibility.proforma.store import ProformaWrite, write_proformas
from feasibility.sourcing import store as sourcing_store


@dataclass(frozen=True, slots=True)
class ProformaCounts:
    """One pro-forma per ranked candidate that has a positive price, so `proformas` is the sum
    of the other three."""

    proformas: int = 0
    proformas_computed: int = 0
    proformas_no_arv: int = 0
    proformas_unsizable: int = 0


def run_proformas(
    connection: Connection, pack: MarketPack, run_id: int, as_of: date
) -> ProformaCounts:
    """Replace the run's pro-formas and merge their counts into the run, in the caller's
    transaction. Serialised with the market's other runs, so a run that rebuilds this one's
    rows cannot do so half way through, and a run that is no longer completed is left alone."""
    sourcing_store.lock_market_runs(connection, pack.market.id)
    if sourcing_store.run_status(connection, run_id) != "completed":
        # A newer attempt has reset or failed this run since the build; its rows are not ours.
        return ProformaCounts()
    gathered = gather_inputs(connection, pack.market.id, run_id, as_of)
    ttl_days = pack.sourcing.estimates.ttl_days
    built = [
        (item, build_proforma(item.inputs, pack.cost_assumptions, estimate_ttl_days=ttl_days))
        for item in gathered
    ]
    write_proformas(connection, run_id, [_row_of(item, result) for item, result in built])

    by_status = Counter(result.status for _, result in built)
    counts = ProformaCounts(
        proformas=len(built),
        proformas_computed=by_status["computed"],
        proformas_no_arv=by_status["no_arv"],
        proformas_unsizable=by_status["unsizable"],
    )
    sourcing_store.merge_counts(connection, run_id, asdict(counts))
    return counts


def _row_of(item: CandidateInputs, result: ProformaResult) -> ProformaWrite:
    """The columns people list and filter by, taken from the result they summarise."""
    totals, offer = result.totals, result.max_offer
    return ProformaWrite(
        candidate_id=item.candidate_id,
        status=result.status,
        reason=result.reason,
        estimate_fetched_on=None if result.arv is None else result.arv.estimate_fetched_on,
        offer_price=item.inputs.price,
        arv=None if result.arv is None else result.arv.arv,
        total_cost=None if totals is None else totals.total_cost,
        profit=None if totals is None else totals.profit,
        margin=None if totals is None else totals.margin,
        roi=None if totals is None else totals.roi,
        annualized_return=None if totals is None else totals.annualized_return,
        max_offer=None if offer is None else offer.max_offer,
        flags=list(result.flags),
        result=result.model_dump(mode="json"),
    )

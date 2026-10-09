"""Read-only views of a run's pro-formas.

This module imports nothing that can write or spend: no RentCast client, no run orchestration,
no estimate spend, no job queue and not the stage that computes the pro-formas. They are made
by the run (command line or worker); this only reads what it stored.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status

from feasibility.api.deps import DEFAULT_PAGE_SIZE, AfterId, EngineDep, Limit
from feasibility.api.routes.sourcing import CandidateId, RunId, require_run
from feasibility.api.schemas import (
    CandidateAddressOut,
    Page,
    ProformaDetailOut,
    ProformaResultOut,
    ProformaSummaryOut,
)
from feasibility.proforma.model import ProformaResult
from feasibility.proforma.store import StoredProforma, read_proforma, read_proformas

router = APIRouter(prefix="/sourcing", tags=["pro-forma"])

ProformaStatus = Literal["computed", "no_arv", "unsizable"]


def _summary(item: StoredProforma) -> dict[str, object]:
    return {
        "candidate_id": item.candidate_id,
        "rank": item.rank,
        "address": CandidateAddressOut(street=item.street, zip5=item.zip5),
        "status": item.status,
        "reason": item.reason,
        "flags": item.flags,
        "offer_price": item.offer_price,
        "arv": item.arv,
        "total_cost": item.total_cost,
        "profit": item.profit,
        "margin": item.margin,
        "roi": item.roi,
        "annualized_return": item.annualized_return,
        "max_offer": item.max_offer,
    }


@router.get("/runs/{run_id}/proformas")
def list_run_proformas(
    engine: EngineDep,
    run_id: RunId,
    proforma_status: Annotated[ProformaStatus | None, Query(alias="status")] = None,
    after: AfterId = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[ProformaSummaryOut]:
    """A run's pro-formas in candidate-rank order, `after` being a rank. Optionally only one
    status. A candidate the run did not rank has none."""
    with engine.connect() as connection:
        require_run(connection, run_id)
        rows = read_proformas(
            connection, run_id, status=proforma_status, after_rank=after, limit=limit + 1
        )
    items = [ProformaSummaryOut.model_validate(_summary(row)) for row in rows[:limit]]
    next_after = str(items[-1].rank) if len(rows) > limit else None
    return Page[ProformaSummaryOut](items=items, next_after=next_after)


@router.get("/runs/{run_id}/candidates/{candidate_id}/proforma")
def get_candidate_proforma(
    engine: EngineDep, run_id: RunId, candidate_id: CandidateId
) -> ProformaDetailOut:
    """One candidate's pro-forma in one run: the summary and every input and intermediate."""
    with engine.connect() as connection:
        found = read_proforma(connection, run_id, candidate_id)
    if found is None or found.result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no pro-forma for this candidate in this run"
        )
    return ProformaDetailOut.model_validate(
        {
            **_summary(found),
            "result": ProformaResultOut.without_addresses(
                ProformaResult.model_validate(found.result)
            ),
        }
    )

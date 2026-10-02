"""Read-only views of the daily sourcing runs.

This module imports nothing that can write or spend: no RentCast client, no run
orchestration, no job queue. Runs are made from the command line or the worker.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import Connection, RowMapping, Select, select

from feasibility.api.deps import (
    DEFAULT_PAGE_SIZE,
    MAX_ID,
    AfterId,
    EngineDep,
    Limit,
    MarketQuery,
)
from feasibility.api.schemas import (
    CandidateAddressOut,
    CandidateDetailOut,
    CandidateListingOut,
    MatchOut,
    Page,
    RunCandidateOut,
    RunOut,
)
from feasibility.sourcing.counts import RunCounts
from feasibility.sourcing.scoring import ScoreBreakdown
from feasibility.tables import (
    candidate,
    listing,
    listing_match,
    run_candidate,
    run_listing,
    sourcing_run,
)

router = APIRouter(prefix="/sourcing", tags=["sourcing"])

RunId = Annotated[int, Path(ge=1, le=MAX_ID)]
CandidateId = Annotated[int, Path(ge=1, le=MAX_ID)]
CandidateStatus = Literal["ranked", "filtered", "unscored"]


def _run_out(row: RowMapping) -> RunOut:
    values = dict(row)
    values["counts"] = RunCounts.model_validate(values["counts"])
    return RunOut.model_validate(values)


RUN_COLUMNS = (
    sourcing_run.c.id,
    sourcing_run.c.market,
    sourcing_run.c.as_of,
    sourcing_run.c.status,
    sourcing_run.c.sync_status,
    sourcing_run.c.counts,
    sourcing_run.c.started_at,
    sourcing_run.c.finished_at,
)


def _require_run(connection: Connection, run_id: int) -> None:
    found = connection.execute(select(sourcing_run.c.id).where(sourcing_run.c.id == run_id)).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")


def _candidate_query(run_id: int) -> Select[tuple[object, ...]]:
    """One row per candidate of a run: the candidate, its primary listing and that
    listing's match. Explicit columns only; the stored raw listing never leaves the database."""
    return (
        select(
            run_candidate.c.candidate_id,
            run_candidate.c.rank,
            run_candidate.c.score,
            run_listing.c.price,
            listing.c.address_line,
            listing.c.zip5,
            run_candidate.c.change_kind,
            run_candidate.c.status,
            run_candidate.c.filter_reasons,
            run_candidate.c.unscored_reason,
            listing_match.c.status.label("match_status"),
            listing_match.c.method.label("match_method"),
            candidate.c.account_id,
            run_candidate.c.breakdown,
        )
        .select_from(
            run_candidate.join(candidate, candidate.c.id == run_candidate.c.candidate_id)
            .join(listing, listing.c.id == run_candidate.c.primary_listing_id)
            .join(
                run_listing,
                (run_listing.c.run_id == run_candidate.c.run_id)
                & (run_listing.c.listing_id == run_candidate.c.primary_listing_id),
            )
            .outerjoin(listing_match, listing_match.c.listing_id == listing.c.id)
        )
        .where(run_candidate.c.run_id == run_id)
    )


def _candidate_fields(values: RowMapping) -> dict[str, Any]:
    return {
        "candidate_id": values["candidate_id"],
        "rank": values["rank"],
        "score": values["score"],
        "price": values["price"],
        "address": CandidateAddressOut(street=values["address_line"], zip5=values["zip5"]),
        "change_kind": values["change_kind"],
        "status": values["status"],
        "filter_reasons": values["filter_reasons"],
        "unscored_reason": values["unscored_reason"],
        "match": (
            None
            if values["match_status"] is None
            else MatchOut(status=values["match_status"], method=values["match_method"])
        ),
        "account_id": values["account_id"],
    }


@router.get("/runs")
def list_runs(
    engine: EngineDep,
    market: MarketQuery = "dallas",
    after: AfterId = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[RunOut]:
    """Runs, most recently created first. `after` is a run id: the next page is older."""
    query = select(*RUN_COLUMNS).where(sourcing_run.c.market == market)
    if after is not None:
        query = query.where(sourcing_run.c.id < after)
    query = query.order_by(sourcing_run.c.id.desc()).limit(limit + 1)

    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    items = [_run_out(row) for row in rows[:limit]]
    next_after = str(items[-1].id) if len(rows) > limit else None
    return Page[RunOut](items=items, next_after=next_after)


@router.get("/runs/{run_id}")
def get_run(engine: EngineDep, run_id: RunId) -> RunOut:
    with engine.connect() as connection:
        row = (
            connection.execute(select(*RUN_COLUMNS).where(sourcing_run.c.id == run_id))
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return _run_out(row)


@router.get("/runs/{run_id}/candidates")
def list_run_candidates(
    engine: EngineDep,
    run_id: RunId,
    candidate_status: Annotated[CandidateStatus, Query(alias="status")] = "ranked",
    after: AfterId = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[RunCandidateOut]:
    """A run's candidates of one status. Ranked ones come in rank order and `after` is a
    rank; the others come in candidate id order and `after` is a candidate id."""
    order_column = (
        run_candidate.c.rank if candidate_status == "ranked" else run_candidate.c.candidate_id
    )
    query = _candidate_query(run_id).where(run_candidate.c.status == candidate_status)
    if after is not None:
        query = query.where(order_column > after)
    query = query.order_by(order_column).limit(limit + 1)

    with engine.connect() as connection:
        _require_run(connection, run_id)
        rows = connection.execute(query).mappings().all()
    items = [RunCandidateOut.model_validate(_candidate_fields(row)) for row in rows[:limit]]
    next_after = None
    if len(rows) > limit:
        last = items[-1]
        next_after = str(last.rank if candidate_status == "ranked" else last.candidate_id)
    return Page[RunCandidateOut](items=items, next_after=next_after)


@router.get("/runs/{run_id}/candidates/{candidate_id}")
def get_run_candidate(
    engine: EngineDep, run_id: RunId, candidate_id: CandidateId
) -> CandidateDetailOut:
    """One candidate in one run: its score breakdown and every listing that run saw for it."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                _candidate_query(run_id).where(run_candidate.c.candidate_id == candidate_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such candidate in this run")
        listing_rows = (
            connection.execute(
                select(
                    run_listing.c.listing_id,
                    listing.c.source,
                    run_listing.c.price,
                    run_listing.c.prev_price,
                    run_listing.c.change_kind,
                    run_listing.c.is_primary,
                )
                .join(listing, listing.c.id == run_listing.c.listing_id)
                .where(run_listing.c.run_id == run_id, run_listing.c.candidate_id == candidate_id)
                .order_by(run_listing.c.listing_id)
            )
            .mappings()
            .all()
        )

    breakdown = row["breakdown"]
    return CandidateDetailOut.model_validate(
        {
            **_candidate_fields(row),
            "breakdown": None if breakdown is None else ScoreBreakdown.model_validate(breakdown),
            "listings": [CandidateListingOut.model_validate(dict(item)) for item in listing_rows],
        }
    )

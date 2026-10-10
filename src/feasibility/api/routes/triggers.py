"""Triggers: the only way the API starts work, and all they do is queue a job.

This module imports the job queue, the payload models and the date check, and nothing that syncs,
spends or calls a model: the worker does that. A trigger is idempotent (a second call for the
same market and date while the first is queued or running answers 200 and queues nothing).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from feasibility.api.deps import MARKET_ID, EngineDep, SettingsDep
from feasibility.jobs import queue
from feasibility.jobs.payloads import MorningRunPayload, RetentionPrunePayload
from feasibility.markets.loader import PackError, get_pack
from feasibility.sourcing import store as sourcing_store
from feasibility.sourcing.dates import resolve_run_date
from feasibility.sourcing.errors import SourcingError

router = APIRouter(prefix="/triggers", tags=["triggers"])


class MorningTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Annotated[str, Field(pattern=MARKET_ID)]
    # Left out, live mode runs for today; mock mode needs a date the snapshot holds.
    as_of: date | None = None


class TriggerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: int | None
    already_active: bool
    market: str
    as_of: date


@router.post("/morning", status_code=status.HTTP_202_ACCEPTED)
def trigger_morning(
    body: MorningTrigger, response: Response, engine: EngineDep, settings: SettingsDep
) -> TriggerOut:
    """Queue the morning chain (source the day, then build its brief) for a market and date."""
    try:
        pack = get_pack(body.market)
    except PackError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown market") from None
    try:
        run_date, _ = resolve_run_date(settings, pack, body.as_of)
    except SourcingError as error:
        # These messages name only dates the snapshot holds or today's date.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    with engine.begin() as connection:
        # Two triggers for different dates must not both pass the date check below.
        sourcing_store.lock_market_runs(connection, pack.market.id)
        latest = sourcing_store.latest_run_as_of(connection, pack.market.id)
        # A morning run already waiting for its turn counts as well: the worker would refuse the
        # earlier date later, and its job would die.
        waiting = queue.latest_active_morning_date(connection, pack.market.id)
        newest = max((d for d in (latest, waiting) if d is not None), default=None)
        if newest is not None and run_date < newest:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"a run for {newest} already started or is queued; an earlier date is refused",
            )
        job_id = queue.enqueue(
            connection,
            "morning.run",
            MorningRunPayload(market=pack.market.id, as_of=run_date),
            dedupe_key=f"morning.run:{pack.market.id}:{run_date.isoformat()}",
        )
    if job_id is None:
        response.status_code = status.HTTP_200_OK
    return TriggerOut(
        job_id=job_id, already_active=job_id is None, market=pack.market.id, as_of=run_date
    )


class RetentionTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Count what would go and delete nothing.
    dry_run: bool = False


class RetentionTriggerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: int | None
    already_active: bool


@router.post("/retention", status_code=status.HTTP_202_ACCEPTED)
def trigger_retention(
    response: Response, engine: EngineDep, body: RetentionTrigger | None = None
) -> RetentionTriggerOut:
    """Queue `retention.prune`, which deletes data past its retention window. One of each kind at a
    time: while a real prune (or a dry run) is queued or running, a second of the same kind answers
    200 and queues nothing."""
    chosen = body or RetentionTrigger()
    with engine.begin() as connection:
        job_id = queue.enqueue(
            connection,
            "retention.prune",
            RetentionPrunePayload(dry_run=chosen.dry_run),
            # Apart: a queued dry run must not make the weekly real prune answer "already active"
            # and silently not happen.
            dedupe_key="retention.prune:dry" if chosen.dry_run else "retention.prune:real",
        )
    if job_id is None:
        response.status_code = status.HTTP_200_OK
    return RetentionTriggerOut(job_id=job_id, already_active=job_id is None)

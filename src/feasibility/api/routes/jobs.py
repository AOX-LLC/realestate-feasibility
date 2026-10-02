from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from feasibility.api.deps import DEFAULT_PAGE_SIZE, EngineDep, Limit
from feasibility.api.schemas import JobOut, Page
from feasibility.tables import job

router = APIRouter(prefix="/jobs", tags=["jobs"])

JobStatus = Literal["queued", "running", "done", "failed", "dead"]


@router.get("")
def list_jobs(
    engine: EngineDep,
    status: JobStatus | None = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[JobOut]:
    """Jobs, newest first. Payloads and error text stay in the database; this shows
    whether a job erred, not what it said."""
    query = select(
        job.c.id,
        job.c.kind,
        job.c.status,
        job.c.attempts,
        job.c.max_attempts,
        job.c.last_error.is_not(None).label("has_error"),
        job.c.run_after,
        job.c.created_at,
        job.c.updated_at,
        job.c.finished_at,
    )
    if status is not None:
        query = query.where(job.c.status == status)
    if after is not None:
        query = query.where(job.c.id < after)
    query = query.order_by(job.c.id.desc()).limit(limit + 1)

    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    items = [JobOut.model_validate(dict(row)) for row in rows[:limit]]
    next_after = str(items[-1].id) if len(rows) > limit else None
    return Page[JobOut](items=items, next_after=next_after)

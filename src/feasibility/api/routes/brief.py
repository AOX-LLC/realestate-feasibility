"""The stored brief of a run. Read-only: it serves what `brief.deliver` built, and nothing here
builds one, so this module imports the stored shape and one read function."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from feasibility.api.deps import EngineDep
from feasibility.api.routes.sourcing import RunId, require_run
from feasibility.delivery.brief import Brief
from feasibility.delivery.store import BriefIntegrityError, read_verified_brief

router = APIRouter(prefix="/sourcing", tags=["brief"])

_FAILED_CHECK = "the stored brief failed its integrity check; build it again"


class BriefOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    content_sha256: str
    built_at: datetime
    brief: Brief


@router.get("/runs/{run_id}/brief")
def get_run_brief(engine: EngineDep, run_id: RunId) -> BriefOut:
    """The run's brief: its computed pro-formas in rank order, with the signals that held and
    the narrative if it passed the figure check. 404 when none has been built."""
    with engine.connect() as connection:
        require_run(connection, run_id)
        try:
            verified = read_verified_brief(connection, run_id)
        except BriefIntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, _FAILED_CHECK) from None
    if verified is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no brief has been built for this run")
    content, stored = verified
    return BriefOut(content_sha256=stored.content_sha256, built_at=stored.built_at, brief=content)

import time

from fastapi import APIRouter, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from feasibility.api.deps import EngineDep, SettingsDep
from feasibility.api.schemas import Health
from feasibility.db import current_schema_version

router = APIRouter(tags=["health"])


@router.get("/health")
def health(
    request: Request, response: Response, engine: EngineDep, settings: SettingsDep
) -> Health:
    """Liveness plus which revision is running. 503 when the database is unreachable."""
    identity = request.app.state.identity
    try:
        with engine.connect() as connection:
            schema_version = current_schema_version(connection)
        service_status = "ok"
    except SQLAlchemyError:
        schema_version = None
        service_status = "degraded"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return Health(
        status=service_status,
        commit=identity.commit,
        commit_source="process_start",
        branch=identity.branch,
        version=identity.version,
        schema_version=schema_version,
        uptime_s=int(time.monotonic() - request.app.state.started_at),
        mode=settings.data_mode.value,
        rentcast_key_configured=settings.rentcast_api_key is not None,
    )

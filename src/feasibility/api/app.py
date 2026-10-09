"""The HTTP API: reads, and triggers that only queue a job. Every route but `/livez` is behind
a bearer token (see `api/gate.py`); nothing here runs a sync, a spend or a model call, because
anything that does can spend money. The worker does that."""

import logging
import time

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from starlette.middleware.base import RequestResponseEndpoint

from feasibility.api.gate import ApiGate
from feasibility.api.identity import read_identity
from feasibility.api.ratelimit import ApiLimits, Clock
from feasibility.api.routes import (
    brief,
    budget,
    health,
    jobs,
    listings,
    llm,
    markets,
    parcels,
    proforma,
    sourcing,
    triggers,
)
from feasibility.config import Settings

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def create_app(settings: Settings, engine: Engine, *, clock: Clock = time.monotonic) -> FastAPI:
    identity = read_identity()
    app = FastAPI(
        title="Real estate feasibility",
        version=identity.version,
        summary=(
            "API over parcels, listings, sourcing, pro-formas, model results, jobs and the "
            "budget, with triggers that queue a job."
        ),
        # No interactive docs and no schema route: they would describe every route to anyone
        # who could reach them. `app.openapi()` still works for tests.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.identity = identity
    app.state.started_at = time.monotonic()

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Added before the headers middleware, so that it sits inside it and a 401, 403 or 429
    # carries the security headers too.
    limits = ApiLimits(settings.api_reads_per_minute, settings.api_triggers_per_hour, clock)
    app.state.limits = limits
    app.add_middleware(ApiGate, settings=settings, limits=limits)
    app.middleware("http")(_security_headers)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)

    for module in (
        health,
        markets,
        parcels,
        listings,
        jobs,
        budget,
        sourcing,
        proforma,
        llm,
        brief,
        triggers,
    ):
        app.include_router(module.router)
    return app


async def _security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    """Say which parameter was wrong without echoing the submitted value back."""
    problems = error.errors() if isinstance(error, RequestValidationError) else []
    detail = [{"loc": problem["loc"], "msg": problem["msg"]} for problem in problems]
    return JSONResponse({"detail": detail}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    # This handler runs outside the middleware stack, so it sets the headers itself.
    return JSONResponse(
        {"detail": "internal error"},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        headers=SECURITY_HEADERS,
    )

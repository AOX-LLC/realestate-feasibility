"""The read-only HTTP API. Nothing here writes: anything that writes can spend the
RentCast budget, so jobs are enqueued from the CLI."""

import logging
import time

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from starlette.middleware.base import RequestResponseEndpoint

from feasibility.api.identity import read_identity
from feasibility.api.routes import budget, health, jobs, listings, markets, parcels, sourcing
from feasibility.config import Settings

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    identity = read_identity()
    app = FastAPI(
        title="Real estate feasibility",
        version=identity.version,
        summary="Read-only API over parcels, listings, sourcing, jobs and the provider budget.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.identity = identity
    app.state.started_at = time.monotonic()

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.middleware("http")(_security_headers)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)

    for module in (health, markets, parcels, listings, jobs, budget, sourcing):
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

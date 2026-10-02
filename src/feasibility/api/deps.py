"""Request-scoped access to what the app factory set up, and shared query parameters."""

from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy import Engine

from feasibility.config import Settings

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MARKET_ID = r"^[a-z][a-z0-9_-]{0,31}$"
# Database ids are bigint; anything larger is rejected as a 422 rather than failing in SQL.
MAX_ID = 2**63 - 1


def engine(request: Request) -> Engine:
    db_engine: Engine = request.app.state.engine
    return db_engine


def settings(request: Request) -> Settings:
    app_settings: Settings = request.app.state.settings
    return app_settings


EngineDep = Annotated[Engine, Depends(engine)]
SettingsDep = Annotated[Settings, Depends(settings)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
AfterId = Annotated[int | None, Query(ge=0, le=MAX_ID)]
MarketQuery = Annotated[str, Query(pattern=MARKET_ID)]

"""Database engine and migration helpers."""

from functools import lru_cache

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine

from feasibility.config import get_settings

MIGRATIONS_LOCATION = "feasibility:migrations"


def create_db_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_settings().database_url)


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade_to_head(engine: Engine) -> None:
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")


def current_schema_version(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()

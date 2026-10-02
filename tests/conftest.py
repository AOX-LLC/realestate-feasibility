import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from feasibility.db import create_db_engine, upgrade_to_head
from feasibility.tables import metadata

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://feasibility:feasibility@127.0.0.1:4502/feasibility_test"
)


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """A database migrated from empty to head once per test session."""
    engine = create_db_engine(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))
    # The session drops the public schema; refuse anything not named as a test database.
    if "test" not in (engine.url.database or ""):
        pytest.exit(f"TEST_DATABASE_URL must name a test database, got {engine.url.database!r}")
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    upgrade_to_head(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    """The migrated database with every table emptied. DELETE is far quicker than
    TRUNCATE on tables this small."""
    with migrated_engine.begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(table.delete())
    return migrated_engine

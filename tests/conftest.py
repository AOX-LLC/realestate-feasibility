import os
from collections.abc import Iterator

import pytest
from llm_fakes import RunModel
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


def empty_database(engine: Engine) -> None:
    """Delete every row of every table. DELETE is far quicker than TRUNCATE on tables this
    small. Modules that build their data once for many tests call this before they do."""
    with engine.begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    """The migrated database with every table emptied."""
    empty_database(migrated_engine)
    return migrated_engine


@pytest.fixture(autouse=True, scope="session")
def quiet_model() -> Iterator[None]:
    """Every sourcing run reaches the model stages, and with no recordings the library's client
    would miss on the first call. So by default a run is given a model that answers every prompt
    plainly and for free. It is patched for the whole session, as module-scoped fixtures run
    sourcing too. A test that wants something else passes `model=` to `run_sourcing`, or puts the
    library's client back with `monkeypatch.setattr("feasibility.llm.run.default_model", ...)`."""
    patch = pytest.MonkeyPatch()
    patch.setattr(
        "feasibility.llm.run.default_model", lambda settings: RunModel(small_cost="0", mid_cost="0")
    )
    yield
    patch.undo()

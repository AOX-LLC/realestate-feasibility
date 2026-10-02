import re

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from feasibility.db import current_schema_version
from feasibility.tables import metadata

PERSONAL_DATA_COLUMN = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.I)


def test_migrations_reach_head(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0001"


def test_table_definitions_match_the_migrations(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), metadata)

    assert differences == []


def test_no_table_can_hold_owner_or_contact_data(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    columns = [
        f"{table}.{column['name']}"
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
    ]

    assert [name for name in columns if PERSONAL_DATA_COLUMN.search(name)] == []

"""Check constraints are named once: `ck_<table>_<name>`, never `ck_<table>_ck_<table>_<name>`."""

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, Column, Engine, Integer, MetaData, Table, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.schema import CreateTable

from feasibility.db import alembic_config, current_schema_version, upgrade_to_head
from feasibility.tables import metadata

MAX_NAME_LENGTH = 63
CHECK_NAMES = text(
    "SELECT c.conrelid::regclass::text, c.conname FROM pg_constraint c "
    "WHERE c.contype = 'c' AND c.connamespace = current_schema()::regnamespace ORDER BY 1, 2"
)


def _check_names(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return [(table, name) for table, name in connection.execute(CHECK_NAMES)]


def _compiled(*names: str) -> str:
    """The DDL the convention makes of `CheckConstraint(..., name=...)` on a table `proforma`."""
    table = Table(
        "proforma",
        MetaData(naming_convention=metadata.naming_convention),
        Column("a", Integer),
        *[CheckConstraint("a > 0", name=name) for name in names],
    )
    return str(CreateTable(table))


def test_a_short_name_gets_the_prefix_and_a_full_one_keeps_it() -> None:
    sql = _compiled("status", "ck_proforma_computed_has_outputs")

    assert "CONSTRAINT ck_proforma_status CHECK" in sql
    assert "CONSTRAINT ck_proforma_computed_has_outputs CHECK" in sql
    assert "ck_proforma_ck_proforma" not in sql


def test_a_name_that_only_starts_like_the_prefix_is_still_prefixed() -> None:
    # Another table's prefix, or the word without the underscore, is part of the name.
    sql = _compiled("ck_listing_status", "ck_proformastatus")

    assert "CONSTRAINT ck_proforma_ck_listing_status CHECK" in sql
    assert "CONSTRAINT ck_proforma_ck_proformastatus CHECK" in sql


def test_an_unnamed_check_constraint_is_still_refused() -> None:
    with pytest.raises(InvalidRequestError, match="explicit name"):
        Table(
            "proforma",
            MetaData(naming_convention=metadata.naming_convention),
            Column("a", Integer),
            CheckConstraint("a > 0"),
        )


def test_a_database_built_from_the_migrations_has_no_doubled_check_name(
    migrated_engine: Engine,
) -> None:
    names = _check_names(migrated_engine)

    assert len(names) > 40
    for table, name in names:
        assert name.startswith(f"ck_{table}_"), name
        assert not name.removeprefix(f"ck_{table}_").startswith(f"ck_{table}_"), name


def _rerun_0008(engine: Engine) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0007")
    upgrade_to_head(engine)


def test_revision_0008_renames_the_doubled_names_a_database_built_before_it_holds(
    migrated_engine: Engine,
) -> None:
    before = _check_names(migrated_engine)
    # Revisions 0006 and 0007 never doubled a name, and a doubled name longer than 63 characters
    # would be shortened by SQLAlchemy to something this revision does not undo (none of the real
    # ones is that long: the longest is 56): put the legacy spelling on every name that can have it.
    legacy = [(t, n) for t, n in before if len(f"ck_{t}_{n}") <= MAX_NAME_LENGTH]
    assert len(legacy) > 20
    try:
        with migrated_engine.begin() as connection:
            for table, name in legacy:
                connection.execute(
                    text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "ck_{table}_{name}"')
                )
        doubled = [n for t, n in _check_names(migrated_engine) if n.startswith(f"ck_{t}_ck_{t}_")]
        assert len(doubled) == len(legacy)

        with migrated_engine.begin() as connection:
            command.downgrade(alembic_config(connection), "0007")
        with migrated_engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")

        assert _check_names(migrated_engine) == before
        with migrated_engine.connect() as connection:
            assert current_schema_version(connection) == "0011"
        # Running it again changes nothing.
        _rerun_0008(migrated_engine)
        assert _check_names(migrated_engine) == before
    finally:
        # Whatever happened, leave the shared database with the names 0008 gives.
        _rerun_0008(migrated_engine)


def test_a_gis_group_lookup_can_use_the_gis_parcel_index(migrated_engine: Engine) -> None:
    """The pro-forma stage reads a group's parcels by `gis_parcel_id`. The demo has 70 parcels, so
    the planner would scan them anyway; with scans switched off it must find the index."""
    with migrated_engine.begin() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN SELECT account_id FROM parcel "
                    "WHERE market = 'dallas' AND gis_parcel_id IN ('SYN000067', 'SYN000068')"
                )
            )
        )

    assert "ix_parcel_market_gis_parcel_id" in plan

import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, insert, inspect
from sqlalchemy.exc import IntegrityError

from feasibility.db import alembic_config, current_schema_version, upgrade_to_head
from feasibility.tables import (
    candidate,
    listing,
    listing_match,
    metadata,
    run_candidate,
    run_listing,
    sourcing_run,
)

PERSONAL_DATA_COLUMN = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.I)


def test_migrations_reach_head(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0007"


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


def test_downgrade_to_0001_and_back_to_head(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0001")
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0001"
        tables = set(inspect(connection).get_table_names())
        assert {
            "sourcing_run",
            "listing_match",
            "candidate",
            "run_listing",
            "candidate_estimate",
            "proforma",
            "llm_call",
            "llm_result",
            "candidate_signals",
            "candidate_narrative",
        }.isdisjoint(tables)
        assert "unit" not in {c["name"] for c in inspect(connection).get_columns("listing")}

    upgrade_to_head(migrated_engine)

    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0007"
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def _listing_id(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(listing)
            .values(
                source="rentcast",
                external_id="check-1",
                market="dallas",
                address_line="1 TEST ST",
                raw={},
            )
            .returning(listing.c.id)
        ).scalar_one()


def _run_id(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="running", sync_status="pending")
            .returning(sourcing_run.c.id)
        ).scalar_one()


def test_a_matched_listing_match_needs_a_method(engine: Engine) -> None:
    listing_id = _listing_id(engine)

    with pytest.raises(IntegrityError, match="method_iff_matched"), engine.begin() as connection:
        connection.execute(
            insert(listing_match).values(listing_id=listing_id, status="matched", street_key="k")
        )


def test_an_unmatched_listing_match_cannot_carry_a_method(engine: Engine) -> None:
    listing_id = _listing_id(engine)

    with pytest.raises(IntegrityError, match="method_iff_matched"), engine.begin() as connection:
        connection.execute(
            insert(listing_match).values(
                listing_id=listing_id, status="unmatched", method="exact", street_key="k"
            )
        )


def _candidate_row(engine: Engine) -> dict[str, object]:
    with engine.begin() as connection:
        candidate_id = connection.execute(
            insert(candidate)
            .values(
                market="dallas", property_key="addr:1", street_key="1|", first_as_of="2026-10-01"
            )
            .returning(candidate.c.id)
        ).scalar_one()
    return {
        "run_id": _run_id(engine),
        "candidate_id": candidate_id,
        "primary_listing_id": _listing_id(engine),
        "change_kind": "new",
    }


def test_a_ranked_row_needs_rank_score_and_breakdown(engine: Engine) -> None:
    row = _candidate_row(engine)

    with pytest.raises(IntegrityError, match="ranked_has_score"), engine.begin() as connection:
        connection.execute(
            insert(run_candidate).values(**row, status="ranked", score=50, breakdown={})
        )


def test_an_unscored_row_needs_a_reason(engine: Engine) -> None:
    row = _candidate_row(engine)

    with pytest.raises(IntegrityError, match="unscored_has_reason"), engine.begin() as connection:
        connection.execute(insert(run_candidate).values(**row, status="unscored"))


def _run_listing_values(engine: Engine, **match: object) -> dict[str, object]:
    return {
        "run_id": _run_id(engine),
        "listing_id": _listing_id(engine),
        "change_kind": "new",
        **match,
    }


@pytest.mark.parametrize(
    ("match", "constraint"),
    [
        ({"match_status": "matched"}, "match_complete"),
        ({"match_status": "unmatched", "match_method": "exact"}, "match_complete"),
        ({"match_status": "unmatched", "match_account_id": "00000000001"}, "match_account"),
        ({"match_status": "bogus"}, "match_status"),
        ({"match_status": "matched", "match_method": "bogus"}, "match_method"),
        # An account id needs a matched status, even when the status is NULL.
        ({"match_account_id": "00000000001"}, "match_account"),
    ],
)
def test_run_listing_rejects_an_inconsistent_match(
    engine: Engine, match: dict[str, object], constraint: str
) -> None:
    values = _run_listing_values(engine, **match)

    with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
        connection.execute(insert(run_listing).values(**values))


@pytest.mark.parametrize(
    "match",
    [
        {},
        {"match_status": "unmatched"},
        {"match_status": "ambiguous"},
        {"match_status": "matched", "match_method": "exact", "match_account_id": "00000000001"},
    ],
)
def test_run_listing_accepts_a_consistent_match(engine: Engine, match: dict[str, object]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(run_listing).values(**_run_listing_values(engine, **match)))


def test_downgrade_to_0002_drops_the_match_columns_and_upgrades_again(
    migrated_engine: Engine,
) -> None:
    match_columns = {"match_status", "match_method", "match_account_id"}
    with migrated_engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0002")
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0002"
        columns = {c["name"] for c in inspect(connection).get_columns("run_listing")}
        assert match_columns.isdisjoint(columns)

    upgrade_to_head(migrated_engine)

    with migrated_engine.connect() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("run_listing")}
        assert match_columns <= columns
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []

"""The proforma table: what its constraints refuse, and that a re-run's cascade clears it."""

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, delete, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from feasibility.db import alembic_config, current_schema_version, upgrade_to_head
from feasibility.tables import (
    candidate,
    listing,
    metadata,
    proforma,
    run_candidate,
    sourcing_run,
)

COMPUTED = {
    "status": "computed",
    "arv": "1000000",
    "total_cost": "900000",
    "profit": "100000",
    "margin": "0.1000",
}
NO_ARV = {"status": "no_arv", "reason": "no_estimate_yet"}


@pytest.fixture
def ranked(engine: Engine) -> dict[str, Any]:
    """A run with one ranked candidate: the key a proforma row hangs from."""
    with engine.begin() as connection:
        run_id = connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="completed", sync_status="fresh")
            .returning(sourcing_run.c.id)
        ).scalar_one()
        candidate_id = connection.execute(
            insert(candidate)
            .values(
                market="dallas", property_key="acct:1", street_key="1", first_as_of="2026-10-01"
            )
            .returning(candidate.c.id)
        ).scalar_one()
        listing_id = connection.execute(
            insert(listing)
            .values(
                source="rentcast",
                external_id="l-1",
                market="dallas",
                address_line="1 TEST ST",
                raw={},
            )
            .returning(listing.c.id)
        ).scalar_one()
        connection.execute(
            insert(run_candidate).values(
                run_id=run_id,
                candidate_id=candidate_id,
                primary_listing_id=listing_id,
                change_kind="new",
                status="ranked",
                score=50,
                rank=1,
                breakdown={},
            )
        )
    return {"run_id": run_id, "candidate_id": candidate_id}


def _row(key: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"offer_price": "400000", "result": {}, **key, **fields}


def _insert(engine: Engine, row: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(proforma).values(**row))


@pytest.mark.parametrize("fields", [COMPUTED, NO_ARV, {**NO_ARV, "reason": "lot_size_missing"}])
def test_a_consistent_row_is_accepted(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any]
) -> None:
    _insert(engine, _row(ranked, **fields))


def test_roi_may_be_blank_on_a_computed_row(engine: Engine, ranked: dict[str, Any]) -> None:
    # A loan that covers every cost leaves no cash invested, so no ROI.
    _insert(engine, _row(ranked, **COMPUTED, roi=None, annualized_return=None))


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"status": "bogus", "reason": "x"}, "ck_proforma_status"),
        # computed needs every output, whichever one is missing
        ({**COMPUTED, "arv": None}, "computed_has_outputs"),
        ({**COMPUTED, "total_cost": None}, "computed_has_outputs"),
        ({**COMPUTED, "profit": None}, "computed_has_outputs"),
        ({**COMPUTED, "margin": None}, "computed_has_outputs"),
        # and a row that is not computed cannot carry all of them
        ({**COMPUTED, "status": "no_arv", "reason": "no_estimate_yet"}, "computed_has_outputs"),
        # a reason says why there is no result, so a computed row has none
        ({**COMPUTED, "reason": "too_few_comps"}, "reason_iff_not_computed"),
        ({"status": "no_arv"}, "reason_iff_not_computed"),
        ({"status": "unsizable"}, "reason_iff_not_computed"),
    ],
)
def test_an_inconsistent_row_is_refused(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _insert(engine, _row(ranked, **fields))


def test_the_offer_price_and_result_are_required(engine: Engine, ranked: dict[str, Any]) -> None:
    for missing in ("offer_price", "result"):
        row = _row(ranked, **COMPUTED)
        row.pop(missing)
        with pytest.raises(IntegrityError, match=missing):
            _insert(engine, row)


def test_a_candidate_has_one_proforma_per_run(engine: Engine, ranked: dict[str, Any]) -> None:
    _insert(engine, _row(ranked, **COMPUTED))

    with pytest.raises(IntegrityError, match="pk_proforma"):
        _insert(engine, _row(ranked, **NO_ARV))


def test_a_proforma_needs_its_run_candidate(engine: Engine, ranked: dict[str, Any]) -> None:
    stranger = {"run_id": ranked["run_id"], "candidate_id": ranked["candidate_id"] + 1}

    with pytest.raises(IntegrityError, match="fk_proforma_run_candidate"):
        _insert(engine, _row(stranger, **COMPUTED))


def test_clearing_the_run_candidate_clears_its_proforma(
    engine: Engine, ranked: dict[str, Any]
) -> None:
    _insert(engine, _row(ranked, **COMPUTED))

    with engine.begin() as connection:
        connection.execute(delete(run_candidate).where(run_candidate.c.run_id == ranked["run_id"]))

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(proforma)).scalar_one() == 0


def test_the_flags_default_to_an_empty_list(engine: Engine, ranked: dict[str, Any]) -> None:
    _insert(engine, _row(ranked, **COMPUTED))

    with engine.connect() as connection:
        assert connection.execute(select(proforma.c.flags)).scalar_one() == []


def test_downgrade_to_0004_drops_the_table_and_upgrades_again(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0004")
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0004"
        assert "proforma" not in inspect(connection).get_table_names()

    upgrade_to_head(migrated_engine)

    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0011"
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []

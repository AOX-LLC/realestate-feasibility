"""The tables of verified model results: what their constraints refuse, that a re-run's cascade
clears them, and that their value lists match the types the code writes."""

from typing import Any, get_args

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, delete, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from feasibility.db import alembic_config, current_schema_version, upgrade_to_head
from feasibility.llm.narrative import NarrativeReason, NarrativeStatus
from feasibility.llm.results import SignalsReason, SignalsStatus
from feasibility.tables import (
    NARRATIVE_REASONS,
    NARRATIVE_STATUSES,
    SIGNALS_REASONS,
    SIGNALS_STATUSES,
    candidate,
    candidate_narrative,
    candidate_signals,
    listing,
    llm_call,
    llm_result,
    metadata,
    run_candidate,
    sourcing_run,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def test_the_value_lists_are_the_types_the_code_writes() -> None:
    assert set(SIGNALS_STATUSES) == set(get_args(SignalsStatus))
    assert set(SIGNALS_REASONS) == set(get_args(SignalsReason))
    assert set(NARRATIVE_STATUSES) == set(get_args(NarrativeStatus))
    assert set(NARRATIVE_REASONS) == set(get_args(NarrativeReason))


@pytest.fixture
def ranked(engine: Engine) -> dict[str, Any]:
    """A run with one ranked candidate: the key the run tables hang from."""
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
    return {"run_id": run_id, "candidate_id": candidate_id, "listing_id": listing_id}


# --- candidate_signals --------------------------------------------------------------------------


def _signals(key: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "run_id": key["run_id"],
        "candidate_id": key["candidate_id"],
        "listing_id": key["listing_id"],
        "status": "extracted",
        "result": {},
        **fields,
    }


def _insert(engine: Engine, table: Any, row: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(table).values(**row))


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"status": "fields_only", "reason": "no_remarks"},
        {"status": "failed", "reason": "provider_error"},
        {"status": "deferred", "reason": "budget"},
    ],
)
def test_a_consistent_signals_row_is_accepted(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any]
) -> None:
    _insert(engine, candidate_signals, _signals(ranked, **fields))


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"status": "bogus", "reason": "budget"}, "ck_candidate_signals_status"),
        ({"status": "deferred", "reason": "bogus"}, "reason_known"),
        # a reason says why nothing was extracted, so an extracted row has none
        ({"reason": "budget"}, "ck_candidate_signals_reason"),
        ({"status": "fields_only"}, "ck_candidate_signals_reason"),
        ({"status": "failed"}, "ck_candidate_signals_reason"),
    ],
)
def test_an_inconsistent_signals_row_is_refused(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _insert(engine, candidate_signals, _signals(ranked, **fields))


def test_a_candidate_has_one_signals_row_per_run(engine: Engine, ranked: dict[str, Any]) -> None:
    _insert(engine, candidate_signals, _signals(ranked))

    with pytest.raises(IntegrityError, match="pk_candidate_signals"):
        _insert(engine, candidate_signals, _signals(ranked))


def test_signals_need_their_run_candidate_and_their_listing(
    engine: Engine, ranked: dict[str, Any]
) -> None:
    stranger = {**ranked, "candidate_id": ranked["candidate_id"] + 1}
    with pytest.raises(IntegrityError, match="fk_candidate_signals_run_candidate"):
        _insert(engine, candidate_signals, _signals(stranger))

    with pytest.raises(IntegrityError, match="fk_candidate_signals_listing_id_listing"):
        _insert(engine, candidate_signals, _signals({**ranked, "listing_id": 0}))


# --- candidate_narrative ------------------------------------------------------------------------


def _narrative(key: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "run_id": key["run_id"],
        "candidate_id": key["candidate_id"],
        "status": "accepted",
        "input_sha256": DIGEST,
        "result": {},
        **fields,
    }


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"status": "rejected", "reason": "figure_check"},
        {"status": "rejected", "reason": "length_check"},
        {"status": "failed", "reason": "refusal"},
        {"status": "deferred", "reason": "llm_not_configured"},
        # no model was configured, so there was no tier to hash the inputs under
        {"status": "deferred", "reason": "llm_not_configured", "input_sha256": None},
        {"status": "not_eligible", "reason": "proforma_no_arv", "input_sha256": None},
    ],
)
def test_a_consistent_narrative_row_is_accepted(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any]
) -> None:
    _insert(engine, candidate_narrative, _narrative(ranked, **fields))


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"status": "bogus", "reason": "budget"}, "ck_candidate_narrative_status"),
        ({"status": "rejected", "reason": "bogus"}, "reason_known"),
        ({"reason": "figure_check"}, "ck_candidate_narrative_reason"),
        ({"status": "rejected"}, "ck_candidate_narrative_reason"),
        ({"input_sha256": "not-a-digest"}, "ck_candidate_narrative_input_sha256"),
        ({"input_sha256": DIGEST.upper()}, "ck_candidate_narrative_input_sha256"),
        # a candidate that was never eligible has no inputs to hash
        (
            {"status": "not_eligible", "reason": "proforma_unsizable"},
            "not_eligible_no_digest",
        ),
    ],
)
def test_an_inconsistent_narrative_row_is_refused(
    engine: Engine, ranked: dict[str, Any], fields: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _insert(engine, candidate_narrative, _narrative(ranked, **fields))


def test_a_candidate_has_one_narrative_row_per_run(engine: Engine, ranked: dict[str, Any]) -> None:
    _insert(engine, candidate_narrative, _narrative(ranked))

    with pytest.raises(IntegrityError, match="pk_candidate_narrative"):
        _insert(engine, candidate_narrative, _narrative(ranked))


def test_a_narrative_needs_its_run_candidate(engine: Engine, ranked: dict[str, Any]) -> None:
    stranger = {**ranked, "candidate_id": ranked["candidate_id"] + 1}

    with pytest.raises(IntegrityError, match="fk_candidate_narrative_run_candidate"):
        _insert(engine, candidate_narrative, _narrative(stranger))


def test_clearing_the_run_candidate_clears_both_run_tables(
    engine: Engine, ranked: dict[str, Any]
) -> None:
    _insert(engine, candidate_signals, _signals(ranked))
    _insert(engine, candidate_narrative, _narrative(ranked))
    _insert(engine, llm_result, _cached())

    with engine.begin() as connection:
        connection.execute(delete(run_candidate).where(run_candidate.c.run_id == ranked["run_id"]))

    with engine.connect() as connection:
        for table in (candidate_signals, candidate_narrative):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
        # The cache is not run-scoped: a rebuilt run reads it.
        assert connection.execute(select(func.count()).select_from(llm_result)).scalar_one() == 1


# --- llm_result ---------------------------------------------------------------------------------


def _cached(**fields: Any) -> dict[str, Any]:
    return {
        "prompt_id": "signals.extract",
        "prompt_version": 1,
        "tier": "small",
        "input_sha256": DIGEST,
        "result": {},
        **fields,
    }


def test_a_cache_entry_is_accepted(engine: Engine) -> None:
    _insert(engine, llm_result, _cached())
    # The same inputs under another tier, prompt version or prompt are another entry.
    _insert(engine, llm_result, _cached(tier="mid"))
    _insert(engine, llm_result, _cached(prompt_version=2))
    _insert(engine, llm_result, _cached(prompt_id="narrative.write"))
    _insert(engine, llm_result, _cached(input_sha256=OTHER_DIGEST))


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"prompt_id": "Not A Prompt"}, "ck_llm_result_prompt_id"),
        ({"prompt_version": 0}, "ck_llm_result_prompt_version"),
        ({"tier": "huge"}, "ck_llm_result_tier"),
        ({"input_sha256": "abc"}, "ck_llm_result_input_sha256"),
    ],
)
def test_a_malformed_cache_entry_is_refused(
    engine: Engine, fields: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _insert(engine, llm_result, _cached(**fields))


def test_a_cache_entry_is_unique_per_prompt_version_tier_and_inputs(engine: Engine) -> None:
    _insert(engine, llm_result, _cached())

    with pytest.raises(IntegrityError, match="pk_llm_result"):
        _insert(engine, llm_result, _cached())


def test_a_cache_entry_outlives_the_call_row_that_made_it(engine: Engine) -> None:
    with engine.begin() as connection:
        call_id = connection.execute(
            insert(llm_call)
            .values(
                stage="signals",
                prompt_id="signals.extract",
                prompt_version=1,
                input_sha256=DIGEST,
                tier="small",
                mode="replay",
                billable=False,
                outcome="provider_error",
                reserved_usd="0.05",
            )
            .returning(llm_call.c.id)
        ).scalar_one()
    _insert(engine, llm_result, _cached(llm_call_id=call_id))

    with engine.begin() as connection:
        connection.execute(delete(llm_call))

    with engine.connect() as connection:
        assert connection.execute(select(llm_result.c.llm_call_id)).scalar_one() is None


# --- the migration ------------------------------------------------------------------------------


def test_downgrade_to_0006_drops_the_tables_and_upgrades_again(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0006")
    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0006"
        tables = set(inspect(connection).get_table_names())
        assert {"llm_result", "candidate_signals", "candidate_narrative"}.isdisjoint(tables)
        assert "llm_call" in tables

    upgrade_to_head(migrated_engine)

    with migrated_engine.connect() as connection:
        assert current_schema_version(connection) == "0009"
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def test_the_new_constraints_carry_no_doubled_table_prefix(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    for table in ("llm_result", "candidate_signals", "candidate_narrative"):
        for constraint in inspector.get_check_constraints(table):
            name = str(constraint["name"])
            assert name.startswith(f"ck_{table}_"), name
            assert not name.startswith(f"ck_{table}_ck_{table}"), name

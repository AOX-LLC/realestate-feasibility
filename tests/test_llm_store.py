"""The SQL of the model stages' results: the cache, the run rows, and the reads of the ledger."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import empty_database
from llm_rows import (
    DIGEST,
    accepted_narrative,
    deferred_narrative,
    extracted_signals,
    fields_only_signals,
    not_eligible_narrative,
    ranked_candidates,
    rejected_narrative,
)
from sqlalchemy import Engine, text, update

from feasibility.config import DataMode, Settings
from feasibility.llm import ledger, store
from feasibility.llm.ledger import LlmCallRecord
from feasibility.snapshot.load import seed
from feasibility.sourcing import store as sourcing_store
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import sourcing_run

OTHER_DIGEST = "b" * 64
DAY_ONE = date(2026, 10, 1)


# --- the cache ----------------------------------------------------------------------------------


def test_a_cached_result_is_found_by_prompt_version_tier_and_inputs(engine: Engine) -> None:
    with engine.begin() as connection:
        store.cache_result(connection, "signals.extract", 1, "small", DIGEST, {"a": 1}, None)

    with engine.connect() as connection:
        assert store.cached_result(connection, "signals.extract", 1, "small", DIGEST) == {"a": 1}
        assert store.cached_result(connection, "signals.extract", 2, "small", DIGEST) is None
        assert store.cached_result(connection, "signals.extract", 1, "mid", DIGEST) is None
        assert store.cached_result(connection, "narrative.write", 1, "small", DIGEST) is None
        assert store.cached_result(connection, "signals.extract", 1, "small", OTHER_DIGEST) is None


def test_caching_the_same_inputs_again_replaces_the_result(engine: Engine) -> None:
    with engine.begin() as connection:
        store.cache_result(connection, "signals.extract", 1, "small", DIGEST, {"a": 1}, None)
        store.cache_result(connection, "signals.extract", 1, "small", DIGEST, {"a": 2}, None)

    with engine.connect() as connection:
        assert store.cached_result(connection, "signals.extract", 1, "small", DIGEST) == {"a": 2}


# --- the run rows -------------------------------------------------------------------------------


def test_a_signals_row_reads_back_as_the_result_that_was_written(engine: Engine) -> None:
    run = ranked_candidates(engine)
    first = run["candidates"][0]
    result = extracted_signals()

    with engine.begin() as connection:
        store.write_signals(
            connection, run["run_id"], first["candidate_id"], first["listing_id"], result
        )

    with engine.connect() as connection:
        assert store.read_signals(connection, run["run_id"], first["candidate_id"]) == result
        assert store.signals_results(connection, run["run_id"]) == {first["candidate_id"]: result}
        assert store.read_signals(connection, run["run_id"], first["candidate_id"] + 1) is None


def test_writing_a_signals_row_again_replaces_it(engine: Engine) -> None:
    run = ranked_candidates(engine)
    first = run["candidates"][0]
    ids = (run["run_id"], first["candidate_id"], first["listing_id"])

    with engine.begin() as connection:
        store.write_signals(connection, *ids, fields_only_signals("no_remarks"))
        store.write_signals(connection, *ids, fields_only_signals("llm_not_configured"))

    with engine.connect() as connection:
        stored = store.read_signals(connection, run["run_id"], first["candidate_id"])
    assert stored is not None
    assert stored.reason == "llm_not_configured"


def test_a_narrative_row_reads_back_and_a_rewrite_replaces_it(engine: Engine) -> None:
    run = ranked_candidates(engine)
    first = run["candidates"][0]

    with engine.begin() as connection:
        store.write_narrative(
            connection, run["run_id"], first["candidate_id"], DIGEST, deferred_narrative()
        )
        store.write_narrative(
            connection, run["run_id"], first["candidate_id"], DIGEST, accepted_narrative()
        )

    with engine.connect() as connection:
        stored = store.read_narrative(connection, run["run_id"], first["candidate_id"])
    assert stored == accepted_narrative()


def test_narrative_lines_page_by_rank_and_filter_by_status(engine: Engine) -> None:
    run = ranked_candidates(engine, 4)
    results = [
        accepted_narrative("First."),
        rejected_narrative(),
        accepted_narrative("Third."),
        not_eligible_narrative(),
    ]
    with engine.begin() as connection:
        for entry, result in zip(run["candidates"], results, strict=True):
            digest = None if result.status == "not_eligible" else DIGEST
            store.write_narrative(connection, run["run_id"], entry["candidate_id"], digest, result)

    with engine.connect() as connection:
        everything = store.narrative_lines(connection, run["run_id"], limit=10)
        second_page = store.narrative_lines(connection, run["run_id"], after_rank=2, limit=10)
        accepted = store.narrative_lines(connection, run["run_id"], status="accepted", limit=10)
        one = store.narrative_lines(connection, run["run_id"], limit=1)

    assert [(line.rank, line.status, line.reason, line.summary) for line in everything] == [
        (1, "accepted", None, "First."),
        (2, "rejected", "figure_check", None),
        (3, "accepted", None, "Third."),
        (4, "not_eligible", "proforma_no_arv", None),
    ]
    assert [line.rank for line in second_page] == [3, 4]
    assert [line.rank for line in accepted] == [1, 3]
    assert [line.rank for line in one] == [1]


def test_a_summary_planted_in_a_rejected_row_is_still_not_served(engine: Engine) -> None:
    run = ranked_candidates(engine, 1)
    entry = run["candidates"][0]
    with engine.begin() as connection:
        store.write_narrative(
            connection, run["run_id"], entry["candidate_id"], DIGEST, rejected_narrative()
        )
        # Written around the model's validator, as a bug or a hand edit could.
        connection.execute(
            text(
                "UPDATE candidate_narrative SET result = jsonb_set(result, '{summary}', "
                "to_jsonb(CAST(:text AS text))) WHERE run_id = :run"
            ),
            {"text": "Planted text that was never accepted.", "run": run["run_id"]},
        )

    with engine.connect() as connection:
        lines = store.narrative_lines(connection, run["run_id"], limit=10)

    assert [(line.status, line.summary) for line in lines] == [("rejected", None)]


def test_a_candidate_is_current_only_for_the_attempt_that_built_the_run(engine: Engine) -> None:
    run = ranked_candidates(engine)
    candidate_id = run["candidates"][0]["candidate_id"]
    with engine.connect() as connection:
        attempt = sourcing_store.run_started_at(connection, run["run_id"])
    assert attempt is not None

    with engine.connect() as connection:
        assert store.candidate_is_current(connection, run["run_id"], candidate_id, attempt)
        assert store.attempt_is_current(connection, run["run_id"], attempt)
        assert not store.candidate_is_current(connection, run["run_id"], candidate_id + 1, attempt)
        assert store.candidate_in_run(connection, run["run_id"], candidate_id)
        assert not store.candidate_in_run(connection, run["run_id"], candidate_id + 1)

    # A newer attempt, even one that has finished and left the run completed.
    with engine.begin() as connection:
        connection.execute(
            update(sourcing_run)
            .where(sourcing_run.c.id == run["run_id"])
            .values(started_at=attempt + timedelta(seconds=1))
        )
    with engine.connect() as connection:
        assert not store.candidate_is_current(connection, run["run_id"], candidate_id, attempt)
        assert not store.attempt_is_current(connection, run["run_id"], attempt)

    with engine.begin() as connection:
        connection.execute(
            update(sourcing_run)
            .where(sourcing_run.c.id == run["run_id"])
            .values(started_at=attempt, status="running")
        )
    with engine.connect() as connection:
        assert not store.candidate_is_current(connection, run["run_id"], candidate_id, attempt)
        assert not store.attempt_is_current(connection, run["run_id"], attempt)


# --- what the stages read ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def day_one(migrated_engine: Engine) -> Iterator[tuple[Engine, int]]:
    empty_database(migrated_engine)
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    seed(migrated_engine, settings)
    result = run_sourcing(migrated_engine, settings, "dallas", DAY_ONE)
    yield migrated_engine, result.run_id
    empty_database(migrated_engine)


def test_signal_sources_are_the_ranked_candidates_in_rank_order(
    day_one: tuple[Engine, int],
) -> None:
    engine, run_id = day_one
    with engine.connect() as connection:
        sources = store.signal_sources(connection, run_id)
        ranked = connection.execute(
            text(
                "SELECT candidate_id, primary_listing_id FROM run_candidate "
                "WHERE run_id = :run AND status = 'ranked' ORDER BY rank"
            ),
            {"run": run_id},
        ).all()

    assert [(s.candidate_id, s.listing_id) for s in sources] == [tuple(r) for r in ranked]
    assert [s.rank for s in sources] == list(range(1, len(sources) + 1))
    assert len(sources) == 12
    assert {s.change_kind for s in sources} == {"new"}
    assert sum(1 for s in sources if s.remarks is None) == 2


def test_narrative_sources_are_the_runs_proformas_in_rank_order(
    day_one: tuple[Engine, int],
) -> None:
    engine, run_id = day_one
    with engine.connect() as connection:
        sources = store.narrative_sources(connection, run_id)

    assert [s.rank for s in sources] == list(range(1, 13))
    assert sorted({s.proforma_status for s in sources}) == ["computed", "no_arv"]
    computed = [s for s in sources if s.proforma_status == "computed"]
    assert len(computed) == 5
    assert all(s.result["status"] == "computed" for s in computed)


# --- the ledger reads ---------------------------------------------------------------------------


def _call(engine: Engine, run_id: int | None, **fields: Any) -> int:
    values: dict[str, Any] = {
        "stage": "signals",
        "prompt_id": "signals.extract",
        "prompt_version": 1,
        "input_sha256": DIGEST,
        "tier": "small",
        "mode": "replay",
        "outcome": "ok",
        "reserved_usd": Decimal("0.05"),
        "cost_usd": Decimal("0.004"),
        "model": "small-model",
        "input_tokens": 900,
        "output_tokens": 100,
        "run_id": run_id,
        **fields,
    }
    return ledger.record_call(engine, LlmCallRecord(**values))


def test_a_runs_cost_is_grouped_by_stage_and_by_model(engine: Engine) -> None:
    run = ranked_candidates(engine)
    run_id = run["run_id"]
    _call(engine, run_id)
    _call(engine, run_id, cost_usd=Decimal("0.006"))
    _call(
        engine, run_id, stage="narrative", tier="mid", model="mid-model", cost_usd=Decimal("0.02")
    )
    # A call that raised: no model, no cost, held at its reservation.
    _call(
        engine,
        run_id,
        outcome="provider_error",
        cost_usd=None,
        model=None,
        input_tokens=None,
        output_tokens=None,
    )
    # Refused before it was sent: not a call.
    _call(
        engine,
        run_id,
        outcome="budget_refused",
        cost_usd=None,
        model=None,
        reserved_usd=Decimal(0),
        input_tokens=None,
        output_tokens=None,
    )
    _call(engine, None)  # another run's, or no run's

    with engine.connect() as connection:
        cost = store.run_cost(connection, run_id)

    assert (cost.total.calls, cost.total.refused) == (4, 1)
    assert cost.total.cost_usd == Decimal("0.030")
    assert cost.total.reserved_unknown_usd == Decimal("0.05")
    assert (cost.total.input_tokens, cost.total.output_tokens) == (2700, 300)
    assert {line.key: (line.calls, line.cost_usd) for line in cost.by_stage} == {
        "signals": (3, Decimal("0.010")),
        "narrative": (1, Decimal("0.02")),
    }
    assert {line.key: line.calls for line in cost.by_model} == {
        "small-model": 2,
        "mid-model": 1,
        None: 1,
    }
    assert cost.modes == ["replay"]


def test_a_run_with_no_calls_costs_nothing(engine: Engine) -> None:
    run = ranked_candidates(engine)

    with engine.connect() as connection:
        cost = store.run_cost(connection, run["run_id"])

    assert (cost.total.calls, cost.total.cost_usd, cost.total.reserved_unknown_usd) == (0, 0, 0)
    assert cost.by_stage == []
    assert cost.by_model == []
    assert cost.modes == []


def test_the_month_counts_billable_calls_only(engine: Engine) -> None:
    noon = datetime(2026, 10, 9, 12, tzinfo=UTC)
    _call(engine, None, mode="record", called_at=noon)
    _call(engine, None, mode="live", called_at=noon, outcome="provider_error", cost_usd=None)
    _call(engine, None, mode="replay", called_at=noon)
    _call(engine, None, mode="record", called_at=datetime(2026, 9, 30, 23, tzinfo=UTC))
    start, end = ledger.month_bounds(noon)

    with engine.connect() as connection:
        spend = store.month_spend(connection, start, end)

    assert spend.calls == 2
    assert spend.cost_usd == Decimal("0.004")
    assert spend.reserved_unknown_usd == Decimal("0.05")

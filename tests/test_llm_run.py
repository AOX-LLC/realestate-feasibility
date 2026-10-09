"""Stages 6 and 7 of the daily run on the snapshot, with a scripted model: what they store, what
they cost, how they fail, and that they leave Phase 3's outputs alone."""

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from aox_agent_core.errors import (
    ModelRefusalError,
    ProviderUnavailableError,
    RateLimitedError,
    ReplayMissError,
    StructuredOutputError,
)
from conftest import empty_database
from llm_fakes import RunModel
from sqlalchemy import Engine, text

from feasibility.config import DataMode, Settings
from feasibility.llm import run as llm_run
from feasibility.llm import store as llm_store
from feasibility.llm.client import build_model_client
from feasibility.llm.run import PermanentModelError, RetryableModelError
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
FREE = {"small_cost": "0", "mid_cost": "0"}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    return migrated_engine


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params).all())


def _signals_by_status(engine: Engine, run_id: int) -> Counter[tuple[str, str | None]]:
    rows = _rows(
        engine, "SELECT status, reason FROM candidate_signals WHERE run_id = :run", run=run_id
    )
    return Counter((row.status, row.reason) for row in rows)


def _ledger(engine: Engine, run_id: int) -> list[Any]:
    return _rows(
        engine,
        "SELECT c.id, c.stage, c.outcome, k.property_key FROM llm_call c "
        "LEFT JOIN candidate k ON k.id = c.candidate_id WHERE c.run_id = :run ORDER BY c.id",
        run=run_id,
    )


def _phase_three(engine: Engine, run_id: int) -> dict[str, list[str]]:
    """Everything Phase 3 stored for the run, as text, so equality is byte for byte."""
    queries = {
        "run_candidate": (
            "SELECT t::text FROM run_candidate t WHERE run_id = :run ORDER BY candidate_id"
        ),
        "run_listing": "SELECT t::text FROM run_listing t WHERE run_id = :run ORDER BY listing_id",
        "proforma": "SELECT t::text FROM proforma t WHERE run_id = :run ORDER BY candidate_id",
        "estimate": "SELECT t::text FROM candidate_estimate t ORDER BY candidate_id, fetched_on",
    }
    return {
        name: [row[0] for row in _rows(engine, sql, run=run_id)] for name, sql in queries.items()
    }


# --- stage 6 on both days ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def both_days(migrated_engine: Engine) -> Iterator[tuple[Engine, SourcingResult, SourcingResult]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = RunModel(**FREE)
    one = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE, model=model)
    two = run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO, model=model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


def test_day_one_stores_signals_for_every_ranked_candidate(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, one, _ = both_days

    assert _signals_by_status(engine, one.run_id) == {
        ("extracted", None): 10,
        ("fields_only", "no_remarks"): 2,
    }
    counts = one.counts
    assert (counts.signals_extracted, counts.signals_fields_only) == (10, 2)
    assert (counts.signals_failed, counts.signals_deferred, counts.signals_reused) == (0, 0, 0)


def test_day_two_stores_signals_for_every_ranked_candidate(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, _, two = both_days

    statuses = _signals_by_status(engine, two.run_id)
    assert sum(statuses.values()) == two.counts.ranked == 17
    assert two.counts.signals_extracted == statuses[("extracted", None)]
    assert two.counts.signals_fields_only == statuses[("fields_only", "no_remarks")]


def test_day_two_calls_the_model_only_for_remarks_it_has_not_read(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, one, two = both_days

    day_one_calls = [row for row in _ledger(engine, one.run_id) if row.stage == "signals"]
    day_two_calls = [row for row in _ledger(engine, two.run_id) if row.stage == "signals"]

    assert len(day_one_calls) == 10
    # The six properties whose remarks day one never sent: the new entrants of day two.
    assert [row.property_key for row in day_two_calls] == [
        "acct:99000000000000015",
        "gis:SYN000067",
        "acct:99000000000000010",
        "acct:99000000000000067",
        "acct:99000000000000008",
        "acct:99000000000000065",
    ]
    assert {row.outcome for row in day_two_calls} == {"ok"}
    assert two.counts.signals_reused == 9
    assert two.counts.signals_reused + len(day_two_calls) == two.counts.signals_extracted == 15
    assert two.counts.llm_calls == 6


def test_the_injection_scan_marks_the_remarks_it_flags(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    _, one, two = both_days

    assert one.counts.signals_suspicious == 1
    assert two.counts.signals_suspicious == 2


def test_a_field_signal_is_stored_beside_the_remarks_signals(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, _, two = both_days
    rows = _rows(
        engine,
        "SELECT k.property_key, s.result FROM candidate_signals s "
        "JOIN candidate k ON k.id = s.candidate_id WHERE s.run_id = :run",
        run=two.run_id,
    )
    cut = [
        row.property_key
        for row in rows
        if any(signal["code"] == "price_reduced" for signal in row.result["signals"])
    ]

    assert cut == ["acct:99000000000000004"]


def test_signals_record_the_primary_listing_that_was_read(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, one, _ = both_days
    rows = _rows(
        engine,
        "SELECT count(*) FROM candidate_signals s JOIN run_candidate rc "
        "ON rc.run_id = s.run_id AND rc.candidate_id = s.candidate_id "
        "WHERE s.run_id = :run AND s.listing_id = rc.primary_listing_id",
        run=one.run_id,
    )

    assert rows[0][0] == 12


# --- re-runs and the cache ---------------------------------------------------------------------


def test_same_day_rerun_makes_no_calls(
    both_days: tuple[Engine, SourcingResult, SourcingResult],
) -> None:
    engine, _, two = both_days
    before = _rows(
        engine,
        "SELECT candidate_id, status, reason FROM candidate_signals WHERE run_id = :run ORDER BY 1",
        run=two.run_id,
    )
    calls_before = len(_ledger(engine, two.run_id))
    model = RunModel(**FREE)

    again = run_sourcing(engine, _settings(), "dallas", DAY_TWO, model=model)

    assert again.run_id == two.run_id
    assert model.calls == []
    assert len(_ledger(engine, two.run_id)) == calls_before
    assert again.counts.llm_calls == 0
    assert again.counts.llm_cost_usd is None
    assert again.counts.signals_reused == again.counts.signals_extracted
    assert (
        _rows(
            engine,
            "SELECT candidate_id, status, reason FROM candidate_signals "
            "WHERE run_id = :run ORDER BY 1",
            run=two.run_id,
        )
        == before
    )


def test_clearing_a_runs_rows_clears_its_signals(seeded: Engine) -> None:
    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=RunModel(**FREE))
    from feasibility.sourcing import store

    with seeded.begin() as connection:
        store.clear_run_rows(connection, result.run_id)

    assert _rows(seeded, "SELECT count(*) FROM candidate_signals")[0][0] == 0
    # The cache outlives the run: a rebuilt run reads it and spends nothing.
    assert _rows(seeded, "SELECT count(*) FROM llm_result")[0][0] == 10
    model = RunModel(**FREE)
    run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)
    assert model.calls == []


# --- caps ---------------------------------------------------------------------------------------


def test_a_run_cap_defers_the_candidates_it_cannot_afford(seeded: Engine) -> None:
    model = RunModel(small_cost="0.05", mid_cost="0.05")
    settings = _settings(llm_run_budget_usd=Decimal("0.12"))

    result = run_sourcing(seeded, settings, "dallas", DAY_ONE, model=model)

    assert len(model.calls) == 2
    assert _signals_by_status(seeded, result.run_id) == {
        ("extracted", None): 2,
        ("deferred", "budget"): 8,
        ("fields_only", "no_remarks"): 2,
    }
    assert result.counts.signals_deferred == 8
    assert result.counts.llm_calls == 2
    ledger = _ledger(seeded, result.run_id)
    assert [row.outcome for row in ledger] == ["ok", "ok", "budget_refused"]
    with seeded.connect() as connection:
        run = connection.execute(text("SELECT status, error FROM sourcing_run")).one()
    assert (run.status, run.error) == ("completed", None)


def test_a_retry_cannot_spend_past_the_run_cap(seeded: Engine) -> None:
    settings = _settings(llm_run_budget_usd=Decimal("0.12"))
    run_sourcing(seeded, settings, "dallas", DAY_ONE, model=RunModel(small_cost="0.05"))

    model = RunModel(small_cost="0.05")
    again = run_sourcing(seeded, settings, "dallas", DAY_ONE, model=model)

    # The two results are cached; the next candidate would pass the cap, so nothing is called.
    assert model.calls == []
    assert again.counts.signals_reused == 2
    assert again.counts.signals_deferred == 8


def test_the_monthly_cap_defers_billable_calls(seeded: Engine) -> None:
    # A zero monthly cap refuses every billable call; replay calls are not billable and pass.
    model = RunModel(**FREE)
    result = run_sourcing(
        seeded, _settings(llm_monthly_budget_usd=Decimal(0)), "dallas", DAY_ONE, model=model
    )

    assert result.counts.signals_extracted == 10


# --- failures -----------------------------------------------------------------------------------


def _fails(error: BaseException) -> Callable[[int], RunModel]:
    return lambda number: RunModel(failures={number: error}, **FREE)


def test_a_provider_error_fails_that_candidate_and_defers_the_rest_then_raises(
    seeded: Engine,
) -> None:
    model = _fails(ProviderUnavailableError("503"))(3)

    with pytest.raises(RetryableModelError):
        run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)

    run_id = _rows(seeded, "SELECT id FROM sourcing_run")[0][0]
    statuses = _signals_by_status(seeded, run_id)
    assert statuses[("extracted", None)] == 2
    assert statuses[("failed", "provider_error")] == 1
    assert statuses[("deferred", "provider_error")] == 7
    assert statuses[("fields_only", "no_remarks")] == 2
    run = _rows(seeded, "SELECT status, error, counts FROM sourcing_run")[0]
    assert run.status == "completed"
    assert run.error is not None
    assert "RetryableModelError" in run.error
    assert run.counts["signals_failed"] == 1
    assert run.counts["signals_deferred"] == 7


def test_a_retry_after_a_provider_error_calls_only_from_the_failed_candidate(
    seeded: Engine,
) -> None:
    with pytest.raises(RetryableModelError):
        run_sourcing(
            seeded, _settings(), "dallas", DAY_ONE, model=_fails(RateLimitedError("429"))(3)
        )

    model = RunModel(**FREE)
    retried = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)

    assert len(model.calls) == 8
    assert retried.counts.signals_extracted == 10
    assert retried.counts.signals_reused == 2
    run = _rows(seeded, "SELECT error FROM sourcing_run")[0]
    assert run.error is None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (StructuredOutputError("bad shape", attempts=[]), "structured_error"),
        (ModelRefusalError("no"), "refusal"),
    ],
)
def test_a_candidate_the_model_cannot_answer_is_failed_and_the_stage_goes_on(
    seeded: Engine, error: BaseException, reason: str
) -> None:
    model = _fails(error)(2)

    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)

    assert _signals_by_status(seeded, result.run_id) == {
        ("extracted", None): 9,
        ("failed", reason): 1,
        ("fields_only", "no_remarks"): 2,
    }
    assert len(model.calls) == 10


def test_a_failed_signals_stage_leaves_ranking_and_proformas_intact(seeded: Engine) -> None:
    clean = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=RunModel(**FREE))
    expected = _phase_three(seeded, clean.run_id)
    assert expected["proforma"], "the comparison must have something to compare"

    for failing in (
        ProviderUnavailableError("503"),
        ReplayMissError("no recording", key="k", path="p"),
    ):
        with seeded.begin() as connection:
            connection.execute(text("DELETE FROM llm_result"))  # or the failing call is never made
        with pytest.raises((RetryableModelError, PermanentModelError)):
            run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=_fails(failing)(1))
        assert _phase_three(seeded, clean.run_id) == expected


def test_a_missing_recording_is_permanent_and_says_nothing_about_the_remarks(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library's own client in replay, with no recordings: the first call misses.
    monkeypatch.setattr("feasibility.llm.run.default_model", build_model_client)

    with pytest.raises(PermanentModelError) as raised:
        run_sourcing(seeded, _settings(), "dallas", DAY_ONE)

    run = _rows(seeded, "SELECT status, error FROM sourcing_run")[0]
    assert run.status == "completed"
    assert run.error is not None
    assert run.error.startswith("PermanentModelError: signals stage stopped at candidate")
    assert "ReplayMissError" in run.error
    assert str(raised.value) in run.error
    # Not one word of a listing's remarks, nor of the library's message, is in what was kept.
    remarks = [
        row[0] for row in _rows(seeded, "SELECT remarks FROM listing WHERE remarks IS NOT NULL")
    ]
    assert not any(remarks[0][:30] in run.error for remarks in [remarks])
    assert _rows(seeded, "SELECT status, count(*) FROM proforma GROUP BY 1 ORDER BY 1") == [
        ("computed", 5),
        ("no_arv", 7),
    ]
    ledger = _ledger(seeded, _rows(seeded, "SELECT id FROM sourcing_run")[0][0])
    assert [row.outcome for row in ledger] == ["replay_error"]


# --- the contract with the database -------------------------------------------------------------


def test_no_model_call_happens_inside_a_transaction(seeded: Engine) -> None:
    open_transactions: list[int] = []

    def probe(prompt_id: str) -> None:
        with seeded.connect() as connection:
            open_transactions.append(
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                    )
                ).scalar_one()
            )

    model = RunModel(before_call=probe, **FREE)
    run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)

    assert len(open_transactions) == len(model.calls) == 10
    assert open_transactions == [0] * 10


def test_a_stage_with_no_model_configured_stores_field_signals_and_makes_no_call(
    seeded: Engine,
) -> None:
    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=RunModel(**FREE))
    ctx = llm_run.stage_context(seeded, _pack(), result.run_id, DAY_ONE, None)

    counts = llm_run.run_signals(ctx, _pack())

    assert (counts["signals_fields_only"], counts["signals_extracted"]) == (12, 0)
    assert _signals_by_status(seeded, result.run_id) == {
        ("fields_only", "no_remarks"): 2,
        ("fields_only", "llm_not_configured"): 10,
    }
    with seeded.connect() as connection:
        stored = llm_store.signals_results(connection, result.run_id)
    assert all(row.extraction is None for row in stored.values())


def _pack() -> Any:
    from feasibility.markets.loader import get_pack

    return get_pack("dallas")

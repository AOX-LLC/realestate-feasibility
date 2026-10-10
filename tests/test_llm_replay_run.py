"""The daily run on the snapshot with the library's own client in replay mode, served by the
committed recordings: no scripted model, no key. What these tests state is what was recorded;
if a prompt, an input or a dependency changes, they fail until the recordings are made again."""

from collections import Counter
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from aox_agent_core import Provider, Usage
from aox_agent_core.models.pricing import cost_of
from conftest import empty_database
from mls_data import KEY_FILE, read_json
from sqlalchemy import Engine, text

from feasibility.config import DataMode, Settings
from feasibility.evals.signals import spans_overlap
from feasibility.llm.client import build_model_client, load_llm_config
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
ACCT = "acct:990000000000000{}"
INJECTED_RECORD = "SYN000103"  # account 015, new on day two


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK, **overrides)  # type: ignore[call-arg]


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params).all())


@pytest.fixture(autouse=True)
def replay_with_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CORE_MODE", raising=False)
    monkeypatch.delenv("AGENT_CORE_ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="module")
def replayed_days(
    migrated_engine: Engine,
) -> Iterator[tuple[Engine, SourcingResult, SourcingResult]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("AGENT_CORE_MODE", raising=False)
        patch.delenv("AGENT_CORE_ANTHROPIC_API_KEY", raising=False)
        settings = _settings()
        model = build_model_client(settings)
        one = run_sourcing(migrated_engine, settings, "dallas", DAY_ONE, model=model)
        two = run_sourcing(migrated_engine, settings, "dallas", DAY_TWO, model=model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


Days = tuple[Engine, SourcingResult, SourcingResult]


def _by_status(engine: Engine, table: str, run_id: int) -> Counter[tuple[str, str | None]]:
    rows = _rows(engine, f"SELECT status, reason FROM {table} WHERE run_id = :run", run=run_id)  # noqa: S608
    return Counter((row.status, row.reason) for row in rows)


# --- both days, served from the recordings ------------------------------------------------------


def test_both_days_finish_with_no_stage_error(replayed_days: Days) -> None:
    engine, one, two = replayed_days

    errors = _rows(engine, "SELECT id, error FROM sourcing_run ORDER BY id")

    assert [row.id for row in errors] == [one.run_id, two.run_id]
    assert [row.error for row in errors] == [None, None]


def test_every_call_of_both_days_hit_a_recording(replayed_days: Days) -> None:
    engine, one, two = replayed_days

    rows = _rows(engine, "SELECT mode, billable, outcome, count(*) FROM llm_call GROUP BY 1, 2, 3")

    assert [(r.mode, r.billable, r.outcome) for r in rows] == [("replay", False, "ok")]
    assert rows[0][3] == one.counts.llm_calls + two.counts.llm_calls == 16 + 8


def test_day_one_signals_and_narratives_are_the_recorded_ones(replayed_days: Days) -> None:
    engine, one, _ = replayed_days

    assert _by_status(engine, "candidate_signals", one.run_id) == {
        ("extracted", None): 10,
        ("fields_only", "no_remarks"): 2,
    }
    assert _by_status(engine, "candidate_narrative", one.run_id) == {
        ("accepted", None): 4,
        ("rejected", "figure_check"): 1,
        ("not_eligible", "proforma_no_arv"): 7,
    }
    counts = one.counts
    assert (counts.signals_extracted, counts.signals_fields_only) == (10, 2)
    assert (counts.narratives_accepted, counts.narratives_rejected) == (4, 1)
    assert (counts.narratives_failed, counts.narratives_deferred) == (0, 0)
    assert counts.signals_suspicious == 1
    assert counts.llm_calls == 16  # ten for the signals, five narratives and one repair
    assert counts.llm_cost_usd == "0.113578"


def test_day_two_signals_and_narratives_are_the_recorded_ones(replayed_days: Days) -> None:
    engine, _, two = replayed_days

    assert _by_status(engine, "candidate_signals", two.run_id) == {
        ("extracted", None): 15,
        ("fields_only", "no_remarks"): 2,
    }
    assert _by_status(engine, "candidate_narrative", two.run_id) == {
        ("accepted", None): 5,
        ("rejected", "figure_check"): 1,
        ("not_eligible", "proforma_no_arv"): 11,
    }
    counts = two.counts
    assert (counts.signals_reused, counts.narratives_reused) == (9, 4)
    assert counts.signals_suspicious == 2
    assert counts.llm_calls == 8
    assert counts.llm_cost_usd == "0.050129"


def test_the_six_computed_candidates_of_day_two_have_the_recorded_statuses(
    replayed_days: Days,
) -> None:
    engine, _, two = replayed_days

    rows = _rows(
        engine,
        "SELECT k.property_key, n.status FROM candidate_narrative n "
        "JOIN candidate k ON k.id = n.candidate_id "
        "WHERE n.run_id = :run AND n.status <> 'not_eligible' ORDER BY k.property_key",
        run=two.run_id,
    )

    assert [(r.property_key, r.status) for r in rows] == [
        (ACCT.format("02"), "accepted"),
        (ACCT.format("04"), "accepted"),
        (ACCT.format("06"), "rejected"),
        (ACCT.format("15"), "accepted"),
        (ACCT.format("51"), "accepted"),
        (ACCT.format("52"), "accepted"),
    ]


def test_a_rejected_narrative_keeps_its_violations_and_no_text(replayed_days: Days) -> None:
    engine, _, two = replayed_days

    row = _rows(
        engine,
        "SELECT n.result FROM candidate_narrative n JOIN candidate k ON k.id = n.candidate_id "
        "WHERE n.run_id = :run AND k.property_key = :key",
        run=two.run_id,
        key=ACCT.format("06"),
    )[0]

    assert row.result["status"] == "rejected"
    assert row.result["summary"] is None
    assert row.result["risks"] == []
    assert row.result["check"]["attempts"] == 2
    assert {v["kind"] for v in row.result["check"]["violations"]} == {"spelled_number"}


def test_a_same_day_rerun_makes_no_call(replayed_days: Days) -> None:
    engine, _, two = replayed_days
    before = _rows(engine, "SELECT count(*) FROM llm_call")[0][0]

    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("AGENT_CORE_MODE", raising=False)
        patch.delenv("AGENT_CORE_ANTHROPIC_API_KEY", raising=False)
        settings = _settings()
        again = run_sourcing(
            engine, settings, "dallas", DAY_TWO, model=build_model_client(settings)
        )

    assert again.run_id == two.run_id
    assert again.counts.llm_calls == 0
    assert (again.counts.signals_reused, again.counts.narratives_reused) == (15, 6)
    # The ledger is never cleared by a re-run; this one added nothing to it.
    assert _rows(engine, "SELECT count(*) FROM llm_call")[0][0] == before


# --- the injected record (account 015, day two) ----------------------------------------------


def _injection_case() -> dict[str, Any]:
    case = read_json(KEY_FILE)[INJECTED_RECORD]["injection"]
    assert case is not None
    return case


def test_the_injected_remarks_are_flagged(replayed_days: Days) -> None:
    engine, _, two = replayed_days

    result = _rows(
        engine,
        "SELECT s.result FROM candidate_signals s JOIN candidate k ON k.id = s.candidate_id "
        "WHERE s.run_id = :run AND k.property_key = :key",
        run=two.run_id,
        key=ACCT.format("15"),
    )[0].result

    assert result["status"] == "extracted"
    assert result["remarks"]["suspicious"] is True
    assert result["remarks"]["suspicious_rules"]


def test_no_signal_of_the_injected_record_quotes_the_injected_span(replayed_days: Days) -> None:
    engine, _, two = replayed_days
    span = _injection_case()["span"]

    row = _rows(
        engine,
        "SELECT s.result, l.remarks FROM candidate_signals s "
        "JOIN candidate k ON k.id = s.candidate_id JOIN listing l ON l.id = s.listing_id "
        "WHERE s.run_id = :run AND k.property_key = :key",
        run=two.run_id,
        key=ACCT.format("15"),
    )[0]
    quotes = [s["quote"] for s in row.result["signals"] if s["source"] == "remarks"]

    assert quotes  # the record still has real signals (seller financing, teardown)
    assert not any(spans_overlap(row.remarks, quote, span) for quote in quotes)


def test_the_canary_reaches_no_stored_result_of_the_injected_record(replayed_days: Days) -> None:
    engine, _, two = replayed_days
    canary = _injection_case()["canary"]

    stored = _rows(
        engine,
        "SELECT s.result::text AS signals, n.result::text AS narrative "
        "FROM candidate_signals s JOIN candidate k ON k.id = s.candidate_id "
        "JOIN candidate_narrative n ON n.run_id = s.run_id AND n.candidate_id = s.candidate_id "
        "WHERE s.run_id = :run AND k.property_key = :key",
        run=two.run_id,
        key=ACCT.format("15"),
    )[0]

    assert canary not in stored.signals
    assert canary not in stored.narrative


# --- cost -----------------------------------------------------------------------------------


def test_a_runs_cost_is_what_its_recorded_tokens_cost_at_the_packaged_prices(
    replayed_days: Days,
) -> None:
    engine, one, two = replayed_days
    config = load_llm_config(_settings())

    for run in (one, two):
        lines = _rows(
            engine,
            "SELECT model, sum(input_tokens) AS tokens_in, sum(output_tokens) AS tokens_out, "
            "sum(cost_usd) AS cost FROM llm_call WHERE run_id = :run GROUP BY model",
            run=run.run_id,
        )
        assert len(lines) == 2  # one small-tier model, one mid-tier model
        total = Decimal(0)
        for line in lines:
            price = config.price_for(Provider.ANTHROPIC, line.model)
            usage = Usage(input_tokens=int(line.tokens_in), output_tokens=int(line.tokens_out))
            assert cost_of(usage, price) == line.cost
            total += line.cost
        assert str(total.quantize(Decimal("0.000001"))) == run.counts.llm_cost_usd


# --- a cap, with the real client --------------------------------------------------------------


def test_a_one_cent_run_budget_defers_every_model_stage_and_leaves_the_pro_formas(
    migrated_engine: Engine,
) -> None:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    settings = _settings(LLM_RUN_BUDGET_USD="0.01")

    result = run_sourcing(
        migrated_engine, settings, "dallas", DAY_ONE, model=build_model_client(settings)
    )

    assert _by_status(migrated_engine, "candidate_signals", result.run_id) == {
        ("fields_only", "no_remarks"): 2,
        ("deferred", "budget"): 10,
    }
    assert _by_status(migrated_engine, "candidate_narrative", result.run_id) == {
        ("deferred", "budget"): 5,
        ("not_eligible", "proforma_no_arv"): 7,
    }
    assert result.counts.llm_calls == 0
    assert (result.counts.proformas_computed, result.counts.proformas_no_arv) == (5, 7)
    assert _rows(migrated_engine, "SELECT error FROM sourcing_run")[0].error is None
    empty_database(migrated_engine)

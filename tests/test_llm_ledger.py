"""The model call ledger: a row per outcome kind, its constraints, and what counts as spend."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.exc import IntegrityError

from feasibility.llm import ledger
from feasibility.llm.ledger import LlmCallRecord
from feasibility.tables import candidate, llm_call, sourcing_run

DIGEST = "a" * 64
RESERVATION = Decimal("0.05")

OK = LlmCallRecord(
    stage="signals",
    prompt_id="signals.extract",
    prompt_version=1,
    input_sha256=DIGEST,
    tier="small",
    mode="replay",
    outcome="ok",
    reserved_usd=RESERVATION,
    model="a-model",
    attempts=2,
    input_tokens=1200,
    output_tokens=300,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=0,
    cost_usd=Decimal("0.002700"),
    latency_ms=840,
)
RAISED = replace(
    OK,
    outcome="provider_error",
    model=None,
    input_tokens=None,
    output_tokens=None,
    cache_creation_input_tokens=None,
    cache_read_input_tokens=None,
    cost_usd=None,
    latency_ms=None,
)


def _row(engine: Engine, row_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(select(llm_call).where(llm_call.c.id == row_id)).mappings().one()
        )


def _new_run(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="running", sync_status="pending")
            .returning(sourcing_run.c.id)
        ).scalar_one()


@pytest.mark.parametrize(
    "call",
    [
        OK,
        replace(OK, mode="record"),
        replace(OK, mode="live"),
        RAISED,
        replace(RAISED, outcome="structured_error", attempts=2),
        replace(RAISED, outcome="refusal"),
        replace(RAISED, outcome="replay_error", mode="replay"),
        replace(RAISED, outcome="budget_refused", reserved_usd=Decimal(0), attempts=1, mode="live"),
    ],
    ids=lambda call: f"{call.outcome}-{call.mode}",
)
def test_every_outcome_writes_one_row_with_the_right_nulls(
    engine: Engine, call: LlmCallRecord
) -> None:
    row = _row(engine, ledger.record_call(engine, call))

    assert row["outcome"] == call.outcome
    assert row["billable"] == (call.mode in ("record", "live"))
    assert row["called_at"] is not None
    assert row["reserved_usd"] == call.reserved_usd
    assert (row["cost_usd"] is not None) == (call.outcome == "ok")
    assert row["model"] == call.model
    assert row["input_tokens"] == call.input_tokens
    assert row["latency_ms"] == call.latency_ms


def test_the_ledger_holds_no_prompt_text_output_or_replay_key() -> None:
    assert {column.name for column in llm_call.columns}.isdisjoint(
        {"prompt", "system", "output", "response", "remarks", "text", "replay_key", "content"}
    )


def test_an_ok_row_without_a_cost_is_refused(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_llm_call_ok_has_cost"):
        ledger.record_call(engine, replace(OK, cost_usd=None))


def test_a_failed_row_with_a_cost_is_refused(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_llm_call_ok_has_cost"):
        ledger.record_call(engine, replace(RAISED, cost_usd=Decimal("0.001")))


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"stage": "chat"}, "ck_llm_call_stage"),
        ({"prompt_id": "Signals Extract"}, "ck_llm_call_prompt_id"),
        ({"prompt_version": 0}, "ck_llm_call_prompt_version"),
        ({"attempts": 0}, "ck_llm_call_attempts"),
        ({"input_sha256": "A" * 64}, "ck_llm_call_input_sha256"),
        ({"tier": "huge"}, "ck_llm_call_tier"),
        ({"mode": "dry"}, "ck_llm_call_mode"),
        ({"outcome": "maybe", "cost_usd": None}, "ck_llm_call_outcome"),
        ({"reserved_usd": Decimal("-0.01")}, "ck_llm_call_nonnegative"),
        ({"input_tokens": -1}, "ck_llm_call_nonnegative"),
    ],
)
def test_the_constraints_refuse_bad_rows(
    engine: Engine, change: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        ledger.record_call(engine, replace(OK, **change))  # type: ignore[arg-type]


def test_constraint_names_are_the_documented_ones(engine: Engine) -> None:
    with engine.connect() as connection:
        names = set(
            connection.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = 'llm_call'::regclass")
            ).scalars()
        )

    assert {
        "pk_llm_call",
        "fk_llm_call_run_id_sourcing_run",
        "fk_llm_call_candidate_id_candidate",
        "ck_llm_call_stage",
        "ck_llm_call_prompt_id",
        "ck_llm_call_tier",
        "ck_llm_call_mode",
        "ck_llm_call_billable",
        "ck_llm_call_outcome",
        "ck_llm_call_nonnegative",
        "ck_llm_call_ok_has_cost",
    } <= names


def test_billable_must_match_the_mode(engine: Engine) -> None:
    with engine.begin() as connection, pytest.raises(IntegrityError, match="ck_llm_call_billable"):
        connection.execute(
            text(
                "INSERT INTO llm_call (stage, prompt_id, prompt_version, input_sha256, tier, "
                "mode, billable, outcome, reserved_usd) VALUES ('signals', 'p', 1, :digest, "
                "'small', 'live', false, 'refusal', 0.05)"
            ),
            {"digest": DIGEST},
        )


def test_run_spend_adds_cost_or_the_reservation_when_the_cost_is_unknown(engine: Engine) -> None:
    run_id = _new_run(engine)
    other_run_id = _new_run_on_another_day(engine)
    for call in (OK, RAISED, replace(OK, cost_usd=Decimal("0.010000"))):
        ledger.record_call(engine, replace(call, run_id=run_id))
    ledger.record_call(engine, replace(OK, run_id=other_run_id))

    # 0.0027 + the 0.05 reservation of the raised call + 0.01
    assert ledger.run_spend(engine, run_id) == Decimal("0.062700")


def _new_run_on_another_day(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-02", status="running", sync_status="pending")
            .returning(sourcing_run.c.id)
        ).scalar_one()


def test_a_run_with_no_calls_has_spent_nothing(engine: Engine) -> None:
    assert ledger.run_spend(engine, _new_run(engine)) == Decimal(0)


def test_a_budget_refused_row_costs_nothing(engine: Engine) -> None:
    run_id = _new_run(engine)
    refused = replace(RAISED, outcome="budget_refused", reserved_usd=Decimal(0), run_id=run_id)

    ledger.record_call(engine, refused)

    assert ledger.run_spend(engine, run_id) == Decimal(0)


def test_monthly_spend_counts_billable_rows_of_this_utc_month_only(engine: Engine) -> None:
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    in_month = [
        replace(OK, mode="record", called_at=datetime(2026, 10, 1, 0, 0, tzinfo=UTC)),
        replace(OK, mode="live", called_at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)),
        replace(RAISED, mode="live", called_at=datetime(2026, 10, 15, tzinfo=UTC)),
    ]
    not_counted = [
        replace(OK, mode="replay", called_at=datetime(2026, 10, 15, tzinfo=UTC)),
        replace(OK, mode="live", called_at=datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)),
        replace(OK, mode="live", called_at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC)),
    ]
    for call in (*in_month, *not_counted):
        ledger.record_call(engine, call)

    # 0.0027 + 0.0027 + the raised call's 0.05 reservation
    assert ledger.billable_spend_in_month(engine, now) == Decimal("0.055400")


def test_the_month_is_utc_whatever_the_zone_of_now() -> None:
    late_in_utc_evening = datetime.fromisoformat("2026-10-31T23:30:00-05:00")  # 04:30 UTC Nov 1

    start, following = ledger.month_bounds(late_in_utc_evening)

    assert (start, following) == (
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert ledger.month_bounds(datetime(2026, 12, 20, tzinfo=UTC))[1] == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_deleting_a_run_or_candidate_keeps_the_row(engine: Engine) -> None:
    run_id = _new_run(engine)
    with engine.begin() as connection:
        candidate_id = connection.execute(
            insert(candidate)
            .values(
                market="dallas", property_key="acct:1", street_key="1", first_as_of="2026-10-01"
            )
            .returning(candidate.c.id)
        ).scalar_one()
    row_id = ledger.record_call(engine, replace(OK, run_id=run_id, candidate_id=candidate_id))

    with engine.begin() as connection:
        connection.execute(sourcing_run.delete().where(sourcing_run.c.id == run_id))
        connection.execute(candidate.delete().where(candidate.c.id == candidate_id))

    row = _row(engine, row_id)
    assert row["run_id"] is None
    assert row["candidate_id"] is None


def test_calls_since_counts_what_one_attempt_made(engine: Engine) -> None:
    run_id = _new_run(engine)
    before = ledger.record_call(engine, replace(OK, run_id=run_id))
    assert ledger.latest_call_id(engine, run_id) == before
    assert ledger.calls_since(engine, run_id, before) == (0, None)

    ledger.record_call(engine, replace(OK, run_id=run_id, cost_usd=Decimal("0.02")))
    ledger.record_call(engine, replace(RAISED, run_id=run_id))
    ledger.record_call(
        engine,
        replace(RAISED, run_id=run_id, outcome="budget_refused", reserved_usd=Decimal(0)),
    )

    # A call that raised counts at its reservation; a refused one was never sent.
    assert ledger.calls_since(engine, run_id, before) == (2, Decimal("0.07"))
    assert ledger.latest_call_id(engine, run_id + 1) == 0

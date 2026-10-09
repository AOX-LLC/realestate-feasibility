"""Spend caps through the metered client: refusal by reservation, which rows count, and the
property that no sequence of calls takes recorded spend past a cap."""

import contextlib
import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from aox_agent_core import AgentCoreConfig, CallResult, Mode
from aox_agent_core.errors import ProviderError
from llm_fakes import INPUTS, PROMPT, FakeClient, Verdict, result
from sqlalchemy import Engine, insert, select

from feasibility.config import Settings
from feasibility.llm import ledger
from feasibility.llm.client import build_model_client
from feasibility.llm.errors import LlmBudgetError
from feasibility.llm.metered import MeteredClient
from feasibility.llm.spend import RunSpendGuard, SessionSpendGuard
from feasibility.tables import llm_call, sourcing_run

RESERVATION = Decimal("0.05")
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> AgentCoreConfig:
    return build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]


def _in_mode(config: AgentCoreConfig, mode: Mode) -> AgentCoreConfig:
    return config.model_copy(update={"mode": mode})


def _run(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="running", sync_status="pending")
            .returning(sourcing_run.c.id)
        ).scalar_one()


def _client(
    engine: Engine,
    config: AgentCoreConfig,
    fake: FakeClient,
    run_id: int,
    *,
    run_cap: str = "0.10",
    monthly_cap: str = "10.00",
) -> MeteredClient:
    guard = RunSpendGuard(
        engine,
        run_id,
        Decimal(run_cap),
        Decimal(monthly_cap),
        billable=config.mode.value in ("record", "live"),
        clock=lambda: NOW,
    )
    return MeteredClient(fake, guard, engine, config, run_id=run_id)


def _call(client: MeteredClient) -> None:
    client.call_sync(PROMPT, inputs=INPUTS, output=Verdict, stage="signals", task="signals_extract")


def _outcomes(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(select(llm_call.c.outcome).order_by(llm_call.c.id)).scalars()
        )


def test_a_run_cap_refuses_the_call_that_could_pass_it_and_writes_a_refused_row(
    engine: Engine, config: AgentCoreConfig
) -> None:
    run_id = _run(engine)
    fake = FakeClient(result("0.060000"), result("0.010000"))
    client = _client(engine, config, fake, run_id, run_cap="0.10")

    _call(client)  # spent 0.00 + 0.05 <= 0.10: allowed, and it cost 0.06
    with pytest.raises(LlmBudgetError, match="run model budget") as refused:
        _call(client)  # spent 0.06 + 0.05 > 0.10

    assert refused.value.scope == "run"
    assert len(fake.calls) == 1
    assert _outcomes(engine) == ["ok", "budget_refused"]
    assert ledger.run_spend(engine, run_id) == Decimal("0.060000")


def test_a_spend_exactly_at_the_cap_is_allowed_and_the_next_call_is_not(
    engine: Engine, config: AgentCoreConfig
) -> None:
    run_id = _run(engine)
    fake = FakeClient(result("0.050000"), result("0.050000"), result("0.050000"))
    client = _client(engine, config, fake, run_id, run_cap="0.10")

    _call(client)
    _call(client)  # 0.05 + 0.05 == 0.10 is not over the cap
    with pytest.raises(LlmBudgetError):
        _call(client)

    assert len(fake.calls) == 2


def test_a_call_that_raised_counts_at_its_reservation(
    engine: Engine, config: AgentCoreConfig
) -> None:
    run_id = _run(engine)
    fake = FakeClient(ProviderError("down"), result())
    client = _client(engine, config, fake, run_id, run_cap="0.09")

    with pytest.raises(ProviderError):
        _call(client)
    with pytest.raises(LlmBudgetError):
        _call(client)  # 0.05 reserved for the failed call + 0.05 > 0.09

    assert ledger.run_spend(engine, run_id) == RESERVATION
    assert _outcomes(engine) == ["provider_error", "budget_refused"]


def test_a_refused_call_adds_nothing_to_what_has_been_spent(
    engine: Engine, config: AgentCoreConfig
) -> None:
    run_id = _run(engine)
    client = _client(engine, config, FakeClient(result("0.080000")), run_id, run_cap="0.10")
    _call(client)
    for _ in range(3):
        with pytest.raises(LlmBudgetError):
            _call(client)

    assert ledger.run_spend(engine, run_id) == Decimal("0.080000")


def test_the_monthly_cap_counts_only_billable_rows(engine: Engine, config: AgentCoreConfig) -> None:
    run_id = _run(engine)
    # 0.0027 of record spend this month, then 0.05 + that > a 0.05 monthly cap.
    ledger.record_call(
        engine,
        ledger.LlmCallRecord(
            stage="eval",
            prompt_id="signals.extract",
            prompt_version=1,
            input_sha256="b" * 64,
            tier="small",
            mode="record",
            outcome="ok",
            reserved_usd=RESERVATION,
            cost_usd=Decimal("0.002700"),
            called_at=NOW,
        ),
    )

    replay = _client(
        engine, _in_mode(config, Mode.REPLAY), FakeClient(result()), run_id, monthly_cap="0.05"
    )
    _call(replay)  # a replay run is not held to the monthly cap

    billable = _client(
        engine, _in_mode(config, Mode.RECORD), FakeClient(result()), run_id, monthly_cap="0.05"
    )
    with pytest.raises(LlmBudgetError, match="monthly model budget") as refused:
        _call(billable)

    assert refused.value.scope == "monthly"


def test_session_cap_stops_at_its_total_with_no_database(config: AgentCoreConfig) -> None:
    guard = SessionSpendGuard(Decimal("0.12"))
    fake = FakeClient(result("0.050000"), result("0.050000"), result("0.050000"))
    client = MeteredClient(fake, guard, None, config)

    for _ in range(2):
        client.call_sync(
            PROMPT, inputs=INPUTS, output=Verdict, stage="eval", task="signals_extract"
        )
    with pytest.raises(LlmBudgetError, match="session model budget"):
        client.call_sync(
            PROMPT, inputs=INPUTS, output=Verdict, stage="eval", task="signals_extract"
        )

    assert guard.spent == Decimal("0.100000")
    assert len(fake.calls) == 2


def test_a_session_guard_takes_an_engine_and_a_monthly_cap_together_or_neither(
    engine: Engine,
) -> None:
    with pytest.raises(ValueError, match="together"):
        SessionSpendGuard(Decimal(1), engine=engine)


@pytest.mark.parametrize("seed", range(8))
def test_no_sequence_of_calls_takes_run_spend_past_the_cap(
    engine: Engine, config: AgentCoreConfig, seed: int
) -> None:
    rng = random.Random(seed)
    cap = Decimal(rng.randrange(5, 60)) / 100
    run_id = _run(engine)
    script: list[CallResult[Verdict] | Exception] = []
    for _ in range(25):
        if rng.random() < 0.25:
            script.append(ProviderError("down"))
        else:
            script.append(result(str(Decimal(rng.randrange(0, 501)) / 10_000)))
    fake = FakeClient(*script)
    client = _client(engine, _in_mode(config, Mode.LIVE), fake, run_id, run_cap=str(cap))

    for _ in script:
        with contextlib.suppress(LlmBudgetError, ProviderError):
            _call(client)
        assert ledger.run_spend(engine, run_id) <= cap

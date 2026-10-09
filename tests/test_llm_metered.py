"""What the metered client writes for each kind of call, and what it forwards."""

import asyncio
import threading
from decimal import Decimal

import pytest
from aox_agent_core import AgentCoreConfig, Mode, Tier
from aox_agent_core.errors import (
    BudgetExceededError,
    ConfigError,
    ModelRefusalError,
    ProviderUnavailableError,
    RateLimitedError,
    ReplayMissError,
    SecretInRecordingError,
    StructuredOutputError,
)
from llm_fakes import INPUTS, PROMPT, FakeClient, Verdict, result
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from feasibility.config import Settings
from feasibility.llm import ledger
from feasibility.llm.client import build_model_client
from feasibility.llm.errors import LlmBudgetError
from feasibility.llm.metered import MeteredClient, input_sha256
from feasibility.llm.spend import SessionSpendGuard
from feasibility.tables import llm_call, sourcing_run

RESERVATION = Decimal("0.05")


@pytest.fixture(scope="module")
def config() -> AgentCoreConfig:
    return build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]


def _metered(
    fake: FakeClient, engine: Engine | None, config: AgentCoreConfig, *, mode: Mode = Mode.REPLAY
) -> MeteredClient:
    return MeteredClient(
        fake,
        SessionSpendGuard(Decimal(100)),
        engine,
        config.model_copy(update={"mode": mode}),
    )


def _rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(r) for r in connection.execute(select(llm_call).order_by(llm_call.c.id)).mappings()
        ]


def _new_run(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="running", sync_status="pending")
            .returning(sourcing_run.c.id)
        ).scalar_one()


def _call(client: MeteredClient, **kwargs: object) -> None:
    client.call_sync(
        PROMPT,
        inputs=INPUTS,
        output=Verdict,
        stage="signals",
        task="signals_extract",
        **kwargs,  # type: ignore[arg-type]
    )


def test_an_ok_call_writes_the_result_and_returns_it_unchanged(
    engine: Engine, config: AgentCoreConfig
) -> None:
    sent = result("0.004000", attempts=2)
    client = _metered(FakeClient(sent), engine, config)

    returned = client.call_sync(
        PROMPT, inputs=INPUTS, output=Verdict, stage="signals", task="signals_extract"
    )

    assert returned is sent
    (row,) = _rows(engine)
    assert (row["stage"], row["prompt_id"], row["prompt_version"], row["tier"]) == (
        "signals",
        "signals.extract",
        1,
        "small",
    )
    assert (row["model"], row["mode"], row["billable"], row["outcome"]) == (
        "a-model",
        "replay",
        False,
        "ok",
    )
    assert (row["input_tokens"], row["output_tokens"]) == (900, 120)
    assert (row["cache_creation_input_tokens"], row["cache_read_input_tokens"]) == (3, 4)
    assert row["cost_usd"] == Decimal("0.004000")
    assert row["reserved_usd"] == RESERVATION
    assert (row["attempts"], row["latency_ms"]) == (2, 812)
    assert row["input_sha256"] == input_sha256(PROMPT, Tier.SMALL, INPUTS)


def test_the_candidate_id_reaches_the_row_and_not_the_wrapped_client(
    engine: Engine, config: AgentCoreConfig
) -> None:
    fake = FakeClient(result())
    client = _metered(fake, engine, config)

    _call(client, candidate_id=None)

    assert "candidate_id" not in fake.calls[0]
    assert fake.calls[0]["task"] == "signals_extract"
    assert fake.calls[0]["tier"] is None


@pytest.mark.parametrize(
    ("error", "outcome", "attempts"),
    [
        (StructuredOutputError("bad", attempts=("a", "b")), "structured_error", 2),
        (ModelRefusalError("no"), "refusal", 1),
        (RateLimitedError("429"), "provider_error", 1),
        (ProviderUnavailableError("503"), "provider_error", 1),
        (ReplayMissError("miss"), "replay_error", 1),
        (SecretInRecordingError("secret"), "replay_error", 1),
        (BudgetExceededError("over"), "budget_refused", 1),  # replay: nothing was sent
    ],
)
def test_every_failure_kind_writes_one_row_and_reraises(
    engine: Engine, config: AgentCoreConfig, error: Exception, outcome: str, attempts: int
) -> None:
    client = _metered(FakeClient(error), engine, config)

    with pytest.raises(type(error)):
        _call(client)

    (row,) = _rows(engine)
    assert row["outcome"] == outcome
    assert row["attempts"] == attempts
    assert row["cost_usd"] is None
    assert row["model"] is None
    assert row["input_tokens"] is None
    assert row["latency_ms"] is None
    assert row["reserved_usd"] == (Decimal(0) if outcome == "budget_refused" else RESERVATION)


def test_a_budget_refusal_from_the_library_in_a_billable_mode_counts_at_the_reservation(
    engine: Engine, config: AgentCoreConfig
) -> None:
    # The library checks again before a retry, after a first attempt that was paid for.
    guard = SessionSpendGuard(Decimal(100))
    client = MeteredClient(
        FakeClient(BudgetExceededError("retry over budget")),
        guard,
        engine,
        config.model_copy(update={"mode": Mode.LIVE}),
    )

    with pytest.raises(BudgetExceededError):
        _call(client)

    (row,) = _rows(engine)
    assert (row["outcome"], row["reserved_usd"]) == ("budget_refused", RESERVATION)
    assert guard.spent == RESERVATION


def test_an_error_the_ledger_does_not_know_is_counted_when_the_call_could_have_cost_money(
    engine: Engine, config: AgentCoreConfig
) -> None:
    billable = _metered(FakeClient(OSError("socket")), engine, config, mode=Mode.LIVE)
    free = _metered(FakeClient(OSError("socket")), engine, config, mode=Mode.REPLAY)

    with pytest.raises(OSError, match="socket"):
        _call(billable)
    with pytest.raises(OSError, match="socket"):
        _call(free)

    (row,) = _rows(engine)
    assert (row["outcome"], row["mode"], row["billable"]) == ("provider_error", "live", True)
    assert row["reserved_usd"] == RESERVATION


def test_a_configuration_error_before_anything_is_sent_leaves_no_row(
    engine: Engine, config: AgentCoreConfig
) -> None:
    client = _metered(FakeClient(), engine, config)

    with pytest.raises(ConfigError, match="Unknown task"):
        client.call_sync(
            PROMPT, inputs=INPUTS, output=Verdict, stage="signals", task="no_such_task"
        )

    assert _rows(engine) == []


def test_naming_a_tier_and_a_task_is_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        _call_with(tier=Tier.MID, task="signals_extract")


def _call_with(**kwargs: object) -> None:
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]
    client = _metered(FakeClient(), None, config)
    client.call_sync(PROMPT, inputs=INPUTS, output=Verdict, stage="signals", **kwargs)  # type: ignore[arg-type]


def test_a_config_without_a_per_call_budget_is_refused(config: AgentCoreConfig) -> None:
    routing = config.routing.model_copy(update={"budget_usd_per_call": None})

    with pytest.raises(ConfigError, match="reservation"):
        MeteredClient(
            FakeClient(),
            SessionSpendGuard(Decimal(1)),
            None,
            config.model_copy(update={"routing": routing}),
        )


def test_a_tier_given_directly_is_the_ledger_tier(engine: Engine, config: AgentCoreConfig) -> None:
    client = _metered(FakeClient(result(tier=Tier.MID)), engine, config)

    client.call_sync(PROMPT, inputs=INPUTS, output=Verdict, stage="narrative", tier=Tier.MID)

    (row,) = _rows(engine)
    assert (row["stage"], row["tier"]) == ("narrative", "mid")


def test_the_digest_covers_prompt_version_tier_and_inputs_and_ignores_key_order() -> None:
    base = input_sha256(PROMPT, Tier.SMALL, {"a": 1, "b": 2})

    assert base == input_sha256(PROMPT, Tier.SMALL, {"b": 2, "a": 1})
    assert (
        len(
            {
                base,
                input_sha256(PROMPT, Tier.MID, {"a": 1, "b": 2}),
                input_sha256(PROMPT, Tier.SMALL, {"a": 1, "b": 3}),
                input_sha256(
                    PROMPT.model_copy(update={"version": 2}), Tier.SMALL, {"a": 1, "b": 2}
                ),
                input_sha256(
                    PROMPT.model_copy(update={"id": "other.prompt"}), Tier.SMALL, {"a": 1, "b": 2}
                ),
            }
        )
        == 5
    )


def test_the_async_path_writes_the_same_rows(engine: Engine, config: AgentCoreConfig) -> None:
    client = _metered(FakeClient(result(), ModelRefusalError("no")), engine, config)

    async def both() -> None:
        await client.call(
            PROMPT, inputs=INPUTS, output=Verdict, stage="eval", task="signals_extract"
        )
        with pytest.raises(ModelRefusalError):
            await client.call(
                PROMPT, inputs=INPUTS, output=Verdict, stage="eval", task="signals_extract"
            )

    asyncio.run(both())

    assert [row["outcome"] for row in _rows(engine)] == ["ok", "refusal"]


def test_the_real_client_in_replay_with_no_recordings_leaves_a_replay_error_row(
    engine: Engine,
) -> None:
    inner = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]
    client = MeteredClient(inner, SessionSpendGuard(Decimal(1)), engine, inner.config)

    with pytest.raises(ReplayMissError):
        _call(client)

    (row,) = _rows(engine)
    assert (row["outcome"], row["mode"], row["billable"]) == ("replay_error", "replay", False)


def test_a_config_whose_mode_differs_from_the_wrapped_clients_is_refused(
    config: AgentCoreConfig,
) -> None:
    inner = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]
    live_config = config.model_copy(update={"mode": Mode.LIVE})

    with pytest.raises(ValueError, match="differs from the client's replay"):
        MeteredClient(inner, SessionSpendGuard(Decimal(1)), None, live_config)


# --- the row exists before the call, and the check and the row are one step ---


def test_the_call_is_counted_while_it_is_in_flight(engine: Engine, config: AgentCoreConfig) -> None:
    run_id = _new_run(engine)
    seen: list[Decimal] = []

    class Probe(FakeClient):
        def call_sync(self, prompt, *, inputs, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(ledger.run_spend(engine, run_id))
            return super().call_sync(prompt, inputs=inputs, **kwargs)

    client = MeteredClient(
        Probe(result("0.004000")),
        SessionSpendGuard(Decimal(100)),
        engine,
        config,
        run_id=run_id,
    )

    _call(client)

    assert seen == [RESERVATION]
    assert ledger.run_spend(engine, run_id) == Decimal("0.004000")


def test_a_row_that_cannot_be_closed_still_counts_at_the_reservation(
    engine: Engine, config: AgentCoreConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _new_run(engine)
    client = MeteredClient(
        FakeClient(result("0.004000")),
        SessionSpendGuard(Decimal(100)),
        engine,
        config,
        run_id=run_id,
    )

    def database_down(*_args: object, **_kwargs: object) -> None:
        raise OSError("connection lost")

    monkeypatch.setattr(ledger, "finish_call", database_down)

    with pytest.raises(OSError, match="connection lost"):
        _call(client)

    assert ledger.run_spend(engine, run_id) == RESERVATION


def test_a_stage_the_ledger_does_not_know_is_refused_before_the_call(
    engine: Engine, config: AgentCoreConfig
) -> None:
    fake = FakeClient(result())
    client = _metered(fake, engine, config)

    with pytest.raises(ValueError, match="unknown stage 'chat'"):
        client.call_sync(
            PROMPT, inputs=INPUTS, output=Verdict, stage="chat", task="signals_extract"
        )

    assert fake.calls == []
    assert _rows(engine) == []


def test_a_candidate_that_does_not_exist_fails_before_the_call_is_made(
    engine: Engine, config: AgentCoreConfig
) -> None:
    fake = FakeClient(result())
    client = _metered(fake, engine, config)

    with pytest.raises(IntegrityError):
        _call(client, candidate_id=987654321)

    assert fake.calls == []
    assert _rows(engine) == []


def test_the_check_waits_for_whoever_holds_the_spend_lock(
    engine: Engine, config: AgentCoreConfig
) -> None:
    fake = FakeClient(result())
    client = _metered(fake, engine, config)
    finished = threading.Event()

    def make_call() -> None:
        _call(client)
        finished.set()

    with engine.begin() as holder:
        ledger.lock_spend(holder)
        worker = threading.Thread(target=make_call)
        worker.start()
        assert not finished.wait(0.5), "the call went ahead while another caller held the lock"
        assert fake.calls == []
    worker.join(timeout=10)

    assert finished.is_set()
    assert len(fake.calls) == 1


def test_last_call_id_is_the_ledger_row_of_the_most_recent_call(
    engine: Engine, config: AgentCoreConfig
) -> None:
    client = _metered(FakeClient(result(), ReplayMissError("miss"), result()), engine, config)
    assert client.last_call_id is None

    _call(client)
    first = client.last_call_id
    with pytest.raises(ReplayMissError):
        _call(client)
    second = client.last_call_id
    _call(client)

    assert [row["id"] for row in _rows(engine)] == [first, second, client.last_call_id]
    assert len({first, second, client.last_call_id}) == 3


def test_last_call_id_is_none_without_a_database(config: AgentCoreConfig) -> None:
    client = _metered(FakeClient(result()), None, config)

    _call(client)

    assert client.last_call_id is None


def test_a_refused_call_leaves_the_id_of_its_budget_refused_row(
    engine: Engine, config: AgentCoreConfig
) -> None:
    client = MeteredClient(
        FakeClient(),
        SessionSpendGuard(Decimal("0.01")),
        engine,
        config,
    )

    with pytest.raises(LlmBudgetError):
        _call(client)

    (row,) = _rows(engine)
    assert row["outcome"] == "budget_refused"
    assert client.last_call_id == row["id"]


def test_last_call_id_is_cleared_when_an_unknown_replay_error_discards_the_row(
    engine: Engine, config: AgentCoreConfig
) -> None:
    client = _metered(FakeClient(RuntimeError("not ours")), engine, config)

    with pytest.raises(RuntimeError):
        _call(client)

    assert _rows(engine) == []
    assert client.last_call_id is None

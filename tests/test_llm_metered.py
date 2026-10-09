"""What the metered client writes for each kind of call, and what it forwards."""

import asyncio
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
from sqlalchemy import Engine, select

from feasibility.config import Settings
from feasibility.llm.client import build_model_client
from feasibility.llm.metered import MeteredClient, input_sha256
from feasibility.llm.spend import SessionSpendGuard
from feasibility.tables import llm_call

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
        (BudgetExceededError("over"), "budget_refused", 1),
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

"""The metered client: every model call passes a spend guard first and writes one ledger row
after, whatever its outcome. It wraps a client (the library's, or a fake in tests) and never
makes a call itself.

No prompt text, remarks or model output is written anywhere here. A call is never made inside a
database transaction: each ledger row commits on its own connection.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from aox_agent_core import AgentCoreConfig, CallResult, PromptRef, Tier
from aox_agent_core.errors import (
    BudgetExceededError,
    ConfigError,
    ModelRefusalError,
    ProviderError,
    ReplayError,
    StructuredOutputError,
)
from pydantic import BaseModel, JsonValue
from sqlalchemy import Engine

from feasibility.llm import ledger
from feasibility.llm.errors import LlmBudgetError
from feasibility.llm.ledger import LlmCallRecord
from feasibility.llm.spend import SpendGuard

OutputT = TypeVar("OutputT", bound=BaseModel)

# Modes whose calls cost money (record writes a recording from a live call).
BILLABLE_MODES = ("record", "live")


class ModelCaller(Protocol):
    """What the metered client needs from the client it wraps: AgentClient has both methods,
    and a test fake supplies either."""

    def call_sync(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        output: type[OutputT],
        tier: Tier | None,
        task: str | None,
        max_attempts: int,
    ) -> CallResult[OutputT]: ...

    async def call(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        output: type[OutputT],
        tier: Tier | None,
        task: str | None,
        max_attempts: int,
    ) -> CallResult[OutputT]: ...


def input_sha256(prompt: PromptRef, tier: Tier, inputs: Mapping[str, JsonValue]) -> str:
    """Our cache key for a call: the prompt, the tier it routes to and its inputs. It is not
    agent-core's replay key, which also covers the prompt text, the schema and the attempt."""
    canonical = json.dumps(
        {
            "prompt_id": prompt.id,
            "prompt_version": prompt.version,
            "tier": tier.value,
            "inputs": inputs,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class _Call:
    """What the ledger needs to know about one call, fixed before it is made."""

    def __init__(
        self,
        *,
        stage: str,
        prompt: PromptRef,
        tier: Tier,
        digest: str,
        run_id: int | None,
        candidate_id: int | None,
    ) -> None:
        self.stage = stage
        self.prompt = prompt
        self.tier = tier
        self.digest = digest
        self.run_id = run_id
        self.candidate_id = candidate_id


class MeteredClient:
    """Wraps a client with a spend guard and the ledger.

    `config` supplies the mode, the task-to-tier routing and the reservation: the per-call
    budget, which agent-core already enforces as the most one call may cost. A config with no
    per-call budget is refused, since there would be nothing to reserve. With no engine (an eval
    run in CI, which has no database) the caps still hold but no rows are written.
    """

    def __init__(
        self,
        inner: ModelCaller,
        guard: SpendGuard,
        engine: Engine | None,
        config: AgentCoreConfig,
        *,
        run_id: int | None = None,
    ) -> None:
        reservation = config.routing.budget_usd_per_call
        if reservation is None:
            raise ConfigError("routing.budget_usd_per_call must be set: it is the reservation.")
        inner_config = getattr(inner, "config", None)
        if isinstance(inner_config, AgentCoreConfig) and inner_config.mode != config.mode:
            # The mode decides whether a call is billable, and so which caps apply and how its
            # row is labelled: it must be the wrapped client's own.
            raise ValueError(
                f"config mode {config.mode.value} differs from the client's "
                f"{inner_config.mode.value}"
            )
        self._inner = inner
        self._guard = guard
        self._engine = engine
        self._config = config
        self._reservation = reservation
        self._billable = config.mode.value in BILLABLE_MODES
        self._run_id = run_id

    def call_sync(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        output: type[OutputT],
        stage: str,
        tier: Tier | None = None,
        task: str | None = None,
        candidate_id: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[OutputT]:
        call = self._prepare(stage, prompt, inputs, tier, task, candidate_id)
        self._reserve(call)
        try:
            result = self._inner.call_sync(
                prompt,
                inputs=inputs,
                output=output,
                tier=tier,
                task=task,
                max_attempts=max_attempts,
            )
        except BaseException as error:
            self._record_failure(call, error)
            raise
        self._record_success(call, result)
        return result

    async def call(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        output: type[OutputT],
        stage: str,
        tier: Tier | None = None,
        task: str | None = None,
        candidate_id: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[OutputT]:
        call = self._prepare(stage, prompt, inputs, tier, task, candidate_id)
        await asyncio.to_thread(self._reserve, call)
        try:
            result = await self._inner.call(
                prompt,
                inputs=inputs,
                output=output,
                tier=tier,
                task=task,
                max_attempts=max_attempts,
            )
        except BaseException as error:
            await asyncio.to_thread(self._record_failure, call, error)
            raise
        await asyncio.to_thread(self._record_success, call, result)
        return result

    def _prepare(
        self,
        stage: str,
        prompt: PromptRef,
        inputs: Mapping[str, JsonValue],
        tier: Tier | None,
        task: str | None,
        candidate_id: int | None,
    ) -> _Call:
        if tier is not None and task is not None:
            raise ValueError("pass tier or task, not both")
        routing = self._config.routing
        resolved = tier or (routing.tier_for_task(task) if task else routing.default_tier)
        return _Call(
            stage=stage,
            prompt=prompt,
            tier=resolved,
            digest=input_sha256(prompt, resolved, inputs),
            run_id=self._run_id,
            candidate_id=candidate_id,
        )

    def _reserve(self, call: _Call) -> None:
        try:
            self._guard.reserve(self._reservation, billable=self._billable)
        except LlmBudgetError:
            self._write(call, outcome="budget_refused", reserved=Decimal(0))
            raise

    def _record_success(self, call: _Call, result: CallResult[Any]) -> None:
        self._write(
            call,
            outcome="ok",
            reserved=self._reservation,
            model=result.model,
            attempts=result.attempts,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_creation_input_tokens=result.usage.cache_creation_input_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
            cost_usd=result.cost_usd,
            latency_ms=round(result.latency_ms),
        )
        self._guard.settle(result.cost_usd)

    def _record_failure(self, call: _Call, error: BaseException) -> None:
        """One row for a call that raised. Its cost is unknown, so it counts at the reservation.
        An error this module does not know, in a billable mode, is counted the same way: a
        request may have gone out, and the total must err high. Such an error in replay, where
        nothing is sent, leaves no row."""
        if isinstance(error, BudgetExceededError):
            # agent-core also checks before a retry, after a first attempt that was paid for and
            # whose cost the error does not carry. In a billable mode that is counted at the
            # reservation; only in replay, where nothing is ever sent, is it free.
            reserved = self._reservation if self._billable else Decimal(0)
            self._write(call, outcome="budget_refused", reserved=reserved)
            self._guard.settle(reserved)
            return
        outcome = _outcome_of(error)
        if outcome is None:
            if not self._billable:
                return
            outcome = "provider_error"
        attempts = len(error.attempts) if isinstance(error, StructuredOutputError) else 1
        self._write(call, outcome=outcome, reserved=self._reservation, attempts=max(1, attempts))
        self._guard.settle(self._reservation)

    def _write(self, call: _Call, *, outcome: str, reserved: Decimal, **measured: Any) -> None:
        if self._engine is None:
            return
        ledger.record_call(
            self._engine,
            LlmCallRecord(
                stage=call.stage,
                prompt_id=call.prompt.id,
                prompt_version=call.prompt.version,
                input_sha256=call.digest,
                tier=call.tier.value,
                mode=self._config.mode.value,
                outcome=outcome,
                reserved_usd=reserved,
                run_id=call.run_id,
                candidate_id=call.candidate_id,
                **measured,
            ),
        )


def _outcome_of(error: BaseException) -> str | None:
    if isinstance(error, StructuredOutputError):
        return "structured_error"
    if isinstance(error, ModelRefusalError):
        return "refusal"
    if isinstance(error, ProviderError):
        return "provider_error"
    if isinstance(error, ReplayError):
        return "replay_error"
    return None

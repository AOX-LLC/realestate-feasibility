"""A scripted stand-in for the model client: no network, no recordings."""

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from aox_agent_core import CallResult, Mode, PromptRef, Provider, Tier, Usage
from pydantic import BaseModel, JsonValue


class Verdict(BaseModel):
    label: str


PROMPT = PromptRef(
    id="signals.extract",
    version=1,
    system="s",
    template="<listing_remarks>${remarks}</listing_remarks>",
)
INPUTS: dict[str, JsonValue] = {"remarks": "Sold as-is."}


def result(
    cost: str = "0.004000",
    *,
    mode: Mode = Mode.REPLAY,
    tier: Tier = Tier.SMALL,
    attempts: int = 1,
    latency_ms: float = 812.4,
) -> CallResult[Verdict]:
    return CallResult(
        output=Verdict(label="x"),
        tier=tier,
        task="signals_extract",
        provider=Provider.ANTHROPIC,
        model="a-model",
        mode=mode,
        usage=Usage(
            input_tokens=900,
            output_tokens=120,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=4,
        ),
        cost_usd=Decimal(cost),
        latency_ms=latency_ms,
        stop_reason="end_turn",
        attempts=attempts,
    )


class FakeClient:
    """Plays a script, one item per call: a CallResult is returned, an exception is raised."""

    def __init__(self, *script: CallResult[Verdict] | BaseException) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def _next(self, prompt: PromptRef, **kwargs: Any) -> CallResult[Verdict]:
        self.calls.append({"prompt": prompt, **kwargs})
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def call_sync(
        self, prompt: PromptRef, *, inputs: Mapping[str, JsonValue], **kwargs: Any
    ) -> CallResult[Verdict]:
        return self._next(prompt, inputs=inputs, **kwargs)

    async def call(
        self, prompt: PromptRef, *, inputs: Mapping[str, JsonValue], **kwargs: Any
    ) -> CallResult[Verdict]:
        return self._next(prompt, inputs=inputs, **kwargs)


class Extractor:
    """A stand-in for the model that answers an extraction by the remarks text it was sent.

    `answers` maps the remarks (the prompt's input) to a `SignalExtraction`, or to an exception
    to raise. A text with no answer raises ReplayMissError, as replay mode does."""

    def __init__(self, answers: Mapping[str, Any], *, cost: str = "0.004000") -> None:
        self._answers = dict(answers)
        self._cost = cost
        self.sent: list[str] = []

    def _answer(self, prompt: PromptRef, inputs: Mapping[str, JsonValue], tier: Tier | None) -> Any:
        from aox_agent_core.errors import ReplayMissError

        remarks = str(inputs["remarks"])
        self.sent.append(remarks)
        if remarks not in self._answers:
            raise ReplayMissError("No recording for this request.", key="k", path="p")
        answer = self._answers[remarks]
        if isinstance(answer, BaseException):
            raise answer
        return CallResult(
            output=answer,
            tier=tier or Tier.SMALL,
            task="signals_extract",
            provider=Provider.ANTHROPIC,
            model="a-model",
            mode=Mode.REPLAY,
            usage=Usage(input_tokens=900, output_tokens=120),
            cost_usd=Decimal(self._cost),
            latency_ms=1.0,
            stop_reason="end_turn",
        )

    def call_sync(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        tier: Tier | None = None,
        **_: Any,
    ) -> Any:
        return self._answer(prompt, inputs, tier)

    async def call(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        tier: Tier | None = None,
        **_: Any,
    ) -> Any:
        return self._answer(prompt, inputs, tier)


class Narrator:
    """A stand-in for the model that plays a script of narrative drafts, one per call, and
    keeps the inputs of every call. An exception in the script is raised."""

    def __init__(self, *script: Any, cost: str = "0.012000") -> None:
        self._script = list(script)
        self._cost = cost
        self.inputs: list[Mapping[str, JsonValue]] = []

    def _next(self, inputs: Mapping[str, JsonValue], tier: Tier | None) -> Any:
        self.inputs.append(inputs)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return CallResult(
            output=item,
            tier=tier or Tier.MID,
            task="narrative_write",
            provider=Provider.ANTHROPIC,
            model="a-mid-model",
            mode=Mode.REPLAY,
            usage=Usage(input_tokens=1500, output_tokens=300),
            cost_usd=Decimal(self._cost),
            latency_ms=1.0,
            stop_reason="end_turn",
        )

    def call_sync(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        tier: Tier | None = None,
        **_: Any,
    ) -> Any:
        return self._next(inputs, tier)

    async def call(
        self,
        prompt: PromptRef,
        *,
        inputs: Mapping[str, JsonValue],
        tier: Tier | None = None,
        **_: Any,
    ) -> Any:
        return self._next(inputs, tier)


class RunModel:
    """A stand-in for the model that answers both of the run's prompts, keeps every call it
    gets and can fail the nth one.

    `extractions` maps the remarks a call was sent to its `SignalExtraction` (any other text gets
    an empty one), or is a function from the remarks to one. `draft` makes a narrative draft from
    the facts and the feedback of a call (default: a plain one that passes the check). `failures`
    maps a 1-based call number to the exception that call raises. `before_call` runs first in
    every call, with the prompt id."""

    def __init__(
        self,
        *,
        extractions: Mapping[str, Any] | Callable[[str], Any] | None = None,
        draft: Any = None,
        failures: Mapping[int, BaseException] | None = None,
        before_call: Any = None,
        small_cost: str = "0.004000",
        mid_cost: str = "0.012000",
    ) -> None:
        self._extractions = extractions if callable(extractions) else dict(extractions or {})
        self._draft = draft
        self._failures = dict(failures or {})
        self._before_call = before_call
        self._costs = {"signals.extract": small_cost, "narrative.write": mid_cost}
        self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []

    def _answer(self, prompt: PromptRef, inputs: Mapping[str, JsonValue]) -> Any:
        from feasibility.llm.narrative_check import NarrativeDraft
        from feasibility.llm.signals import SignalExtraction

        if self._before_call is not None:
            self._before_call(prompt.id)
        self.calls.append((prompt.id, inputs))
        number = len(self.calls)
        if number in self._failures:
            raise self._failures[number]
        if prompt.id == "signals.extract":
            remarks = str(inputs["remarks"])
            found = (
                self._extractions(remarks)
                if callable(self._extractions)
                else self._extractions.get(remarks)
            )
            if isinstance(found, BaseException):
                raise found
            output = found or SignalExtraction(signals=[], injection_suspected=False)
            tier, task = Tier.SMALL, "signals_extract"
        else:
            output = (
                self._draft(inputs["facts"], inputs["feedback"])
                if self._draft is not None
                else NarrativeDraft(
                    summary="A plain summary of where this candidate stands.",
                    risks=[],
                    checks_before_offer=[],
                )
            )
            if isinstance(output, BaseException):
                raise output
            tier, task = Tier.MID, "narrative_write"
        return CallResult(
            output=output,
            tier=tier,
            task=task,
            provider=Provider.ANTHROPIC,
            model="a-model",
            mode=Mode.REPLAY,
            usage=Usage(input_tokens=900, output_tokens=120),
            cost_usd=Decimal(self._costs[prompt.id]),
            latency_ms=1.0,
            stop_reason="end_turn",
        )

    def call_sync(self, prompt: PromptRef, *, inputs: Mapping[str, JsonValue], **_: Any) -> Any:
        return self._answer(prompt, inputs)

    async def call(self, prompt: PromptRef, *, inputs: Mapping[str, JsonValue], **_: Any) -> Any:
        return self._answer(prompt, inputs)

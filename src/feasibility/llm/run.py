"""Stages 6 and 7 of a sourcing run: the signals of each ranked candidate, then its risk narrative.

Both stages run after the ranking, the estimates and the pro-formas are stored, and neither can
change any of them: they read those tables and write only `candidate_signals`,
`candidate_narrative`, the cache `llm_result`, the ledger and the run's counts.

No model call is made inside a database transaction. A stage reads what it needs in one short
read, then goes candidate by candidate: the call (the metered client writes its ledger rows on
its own connections), the verification, and one short transaction that stores the candidate's
row. A candidate whose inputs were seen before is served from the cache and costs nothing, so a
re-run or a job retry spends only on what changed or never finished.

How a stage fails, per candidate:

* a cap refuses a call (`LlmBudgetError`): the candidate and every later one that is not in the
  cache are `deferred` / `budget`; the stage ends normally;
* the model refuses, or its answer does not fit the schema, or the library finds a call over its
  per-call budget: that candidate is `failed`; the stage goes on;
* the provider fails (a 429, a 5xx): that candidate is `failed`, every later one that is not in
  the cache is `deferred`, and the stage raises `RetryableModelError` so the job runs again (and
  reuses everything that finished);
* a recording is missing, stale or malformed, or the model is misconfigured: the stage stops with
  `PermanentModelError`; a retry cannot fix it.

What a failure says is built here from the error's class and never from its text, so nothing the
model, the provider or a listing said can reach `sourcing_run.error` or a log line.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal, assert_never, cast

from aox_agent_core.errors import (
    AgentCoreError,
    BudgetExceededError,
    ModelRefusalError,
    ProviderError,
    ProviderRequestError,
    ReplayError,
    StructuredOutputError,
)
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, text

from feasibility.config import Settings
from feasibility.llm import ledger
from feasibility.llm import store as llm_store
from feasibility.llm.catalogue import SignalCode
from feasibility.llm.client import build_model_client, load_llm_config
from feasibility.llm.errors import LlmBudgetError
from feasibility.llm.facts import Facts, build_facts
from feasibility.llm.field_signals import FieldSignalInput, field_signals
from feasibility.llm.metered import MeteredClient, ModelCaller, input_sha256
from feasibility.llm.narrative import (
    NARRATIVE_PROMPT,
    NarrativeReason,
    NarrativeResult,
    StoredFacts,
    accepted_result,
    narrative_inputs,
    rejected_result,
    write_narrative_sync,
)
from feasibility.llm.narrative import PROMPT_ID as NARRATIVE_PROMPT_ID
from feasibility.llm.narrative import PROMPT_VERSION as NARRATIVE_PROMPT_VERSION
from feasibility.llm.narrative import TASK as NARRATIVE_TASK
from feasibility.llm.narrative_check import NarrativeDraft, RiskPoint, check_narrative
from feasibility.llm.results import (
    CachedExtraction,
    ExtractionInfo,
    RemarksInfo,
    SignalsReason,
    SignalsResult,
    StoredSignal,
)
from feasibility.llm.signals import (
    EXTRACT_PROMPT,
    ExtractionInput,
    SignalClaim,
    SignalExtraction,
    build_extraction_input,
    extraction_inputs,
    verify_extraction,
)
from feasibility.llm.signals import PROMPT_ID as SIGNALS_PROMPT_ID
from feasibility.llm.signals import PROMPT_VERSION as SIGNALS_PROMPT_VERSION
from feasibility.llm.signals import TASK as SIGNALS_TASK
from feasibility.llm.spend import RunSpendGuard
from feasibility.llm.untrusted import HIDDEN_TEXT_MARKER, InjectionHit
from feasibility.markets.schema import MarketPack
from feasibility.proforma.model import ProformaResult
from feasibility.sourcing import store as sourcing_store

log = logging.getLogger(__name__)

SIGNALS_STAGE = "signals"
NARRATIVE_STAGE = "narrative"
REDACTION_TOKEN = "[contact removed]"  # noqa: S105 (the redactor's replacement text)


class ModelStageError(RuntimeError):
    """A model stage did not finish. The ranking, the estimates and the pro-formas are intact."""


class RetryableModelError(ModelStageError):
    """The provider failed; running the job again may finish what is left."""


class PermanentModelError(ModelStageError):
    """A recording is missing or the model is misconfigured; running the job again cannot help."""


class SupersededError(PermanentModelError):
    """A newer attempt of the run has taken it over, so this attempt stopped without writing
    more. The job is not retried: the newer attempt owns the run."""


def default_model(settings: Settings) -> ModelCaller:
    """The model a run calls when none is passed in: the library's client in the settings' mode.
    Tests replace this name."""
    return build_model_client(settings)


def open_client(
    engine: Engine, settings: Settings, run_id: int, model: ModelCaller | None
) -> MeteredClient | None:
    """The run's metered client, or None when live data has no model configured."""
    if not settings.llm_configured:
        return None
    try:
        return MeteredClient(
            model if model is not None else default_model(settings),
            RunSpendGuard(
                engine, run_id, settings.llm_run_budget_usd, settings.llm_monthly_budget_usd
            ),
            engine,
            load_llm_config(settings),
            run_id=run_id,
        )
    except (AgentCoreError, ValueError, OSError) as error:
        # A missing or malformed config file, a missing per-call budget, a mode the client does not
        # have: the same on every attempt, so the job is not retried.
        raise PermanentModelError(
            f"the model client could not be built: {type(error).__name__}"
        ) from None


# --- what a stage works with --------------------------------------------------------------------


@dataclass
class Blocked:
    """Why no further call is made this stage: a cap (`budget`) or a provider failure
    (`provider_error`). Candidates already in the cache are still served."""

    reason: str | None = None
    provider_failed: bool = False


@dataclass(frozen=True)
class StageContext:
    """The run a stage works for. `client` is None when no model is configured; `watermark` is
    the newest ledger row the run had before this attempt, so the attempt's own calls can be
    counted."""

    engine: Engine
    market: str
    run_id: int
    run_date: date
    client: MeteredClient | None
    watermark: int
    # When the attempt of the run that these stages belong to started. A newer attempt resets it,
    # and from then on this one writes nothing into the run.
    attempt: datetime
    # Why no further call is made in this attempt, shared by the stages: a cap that refused a
    # signals call refuses a narrative call too (every call holds the same reservation).
    blocked: Blocked


def current_attempt(engine: Engine, run_id: int) -> datetime:
    """The marker of the run's current attempt, to be read when the attempt starts."""
    with engine.connect() as connection:
        attempt = sourcing_store.run_started_at(connection, run_id)
    if attempt is None:
        raise ValueError(f"no run {run_id}")
    return attempt


def stage_context(
    engine: Engine,
    pack: MarketPack,
    run_id: int,
    run_date: date,
    client: MeteredClient | None,
    attempt: datetime,
) -> StageContext:
    return StageContext(
        engine,
        pack.market.id,
        run_id,
        run_date,
        client,
        ledger.latest_call_id(engine, run_id),
        attempt,
        Blocked(),
    )


@contextmanager
def _one_spender(engine: Engine, market: str) -> Iterator[None]:
    """Hold the session-level lock that lets one model stage per market work at a time, so a
    retry that overlaps its predecessor cannot spend against the same caps twice. No transaction
    stays open while it is held."""
    key = {"key": f"llm_spend:{market}"}
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), key)
        connection.commit()
        try:
            yield
        finally:
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), key)
            connection.commit()


@dataclass(frozen=True)
class Failure:
    """How a stage treats an error from a call: the reason it stores, and what happens next."""

    reason: str
    action: Literal["candidate", "budget", "provider", "permanent"]


def classify(error: Exception) -> Failure | None:
    """None for an error this module does not know; the caller lets it propagate."""
    if isinstance(error, LlmBudgetError):
        return Failure("budget", "budget")
    if isinstance(error, BudgetExceededError):
        # One call's worst case was over the per-call budget: this candidate's input, not the run.
        return Failure("budget", "candidate")
    if isinstance(error, StructuredOutputError):
        return Failure("structured_error", "candidate")
    if isinstance(error, ModelRefusalError):
        return Failure("refusal", "candidate")
    if isinstance(error, ProviderRequestError):
        # The provider rejected this input as invalid: another attempt gets the same answer, and
        # the candidates after it are not affected.
        return Failure("provider_error", "candidate")
    if isinstance(error, ProviderError):
        return Failure("provider_error", "provider")
    if isinstance(error, ReplayError):
        return Failure("replay_error", "permanent")
    if isinstance(error, AgentCoreError):
        # Configuration, routing, credentials, prompt: the same on every attempt.
        return Failure("replay_error", "permanent")
    return None


def _permanent(stage: str, candidate_id: int, error: Exception) -> PermanentModelError:
    return PermanentModelError(
        f"{stage} stage stopped at candidate {candidate_id}: {type(error).__name__} "
        "(a recording is missing or the model is misconfigured)"
    )


def _superseded(stage: str) -> SupersededError:
    return SupersededError(f"{stage} stage: a newer attempt of this run has taken over")


def _retryable(stage: str) -> RetryableModelError:
    return RetryableModelError(f"{stage} stage: the model provider failed; the job will retry")


def _write(ctx: StageContext, candidate_id: int, write: Callable[[Connection], None]) -> bool:
    """Store one candidate's row in its own short transaction, serialised with the market's
    runs. False, and nothing written, when a newer attempt of the run has reset it since."""
    with ctx.engine.begin() as connection:
        sourcing_store.lock_market_runs(connection, ctx.market)
        if not llm_store.candidate_is_current(connection, ctx.run_id, candidate_id, ctx.attempt):
            return False
        write(connection)
    return True


def _merge_counts(ctx: StageContext, tally: dict[str, int]) -> dict[str, Any]:
    """Merge the stage's counts, with the calls this attempt has made so far and what they cost,
    into the run, when the run is still the one that was built. Returns what it merged. Best
    effort: the stage's own failure must propagate, not this one."""
    patch: dict[str, Any] = dict(tally)
    try:
        calls, cost = ledger.calls_since(ctx.engine, ctx.run_id, ctx.watermark)
        patch["llm_calls"] = calls
        patch["llm_cost_usd"] = None if cost is None else str(cost)
        with ctx.engine.begin() as connection:
            sourcing_store.lock_market_runs(connection, ctx.market)
            if llm_store.attempt_is_current(connection, ctx.run_id, ctx.attempt):
                sourcing_store.merge_counts(connection, ctx.run_id, patch)
    except Exception:
        log.exception("could not record the model counts of run %s", ctx.run_id)
    return patch


# --- stage 6: signals ---------------------------------------------------------------------------


@dataclass
class SignalTally:
    signals_extracted: int = 0
    signals_fields_only: int = 0
    signals_failed: int = 0
    signals_deferred: int = 0
    signals_reused: int = 0
    signals_quotes_dropped: int = 0
    signals_suspicious: int = 0

    def add(self, result: SignalsResult) -> None:
        match result.status:
            case "extracted":
                self.signals_extracted += 1
            case "fields_only":
                self.signals_fields_only += 1
            case "failed":
                self.signals_failed += 1
            case "deferred":
                self.signals_deferred += 1
        self.signals_quotes_dropped += len(result.dropped)
        if result.remarks is not None and result.remarks.suspicious:
            self.signals_suspicious += 1
        if result.extraction is not None and result.extraction.reused:
            self.signals_reused += 1


def _scan_fingerprint(hits: Sequence[InjectionHit]) -> str:
    canonical = json.dumps(sorted((hit.rule, hit.start, hit.end) for hit in hits))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _remarks_info(remarks: str, extraction_input: ExtractionInput) -> RemarksInfo:
    """The remarks that were read, by hash and size. Only the stored text is known here, so the
    invisible-character count is the number of visible markers the ingestion left in it."""
    return RemarksInfo(
        sha256=hashlib.sha256(remarks.encode()).hexdigest(),
        char_count=len(remarks),
        redaction_count=remarks.count(REDACTION_TOKEN),
        removed_invisible_count=remarks.count(HIDDEN_TEXT_MARKER),
        suspicious=extraction_input.suspicious,
        suspicious_rules=extraction_input.rules,
    )


def _without_extraction(
    status: Literal["fields_only", "failed", "deferred"],
    reason: str,
    fields: list[StoredSignal],
    remarks: RemarksInfo | None = None,
) -> SignalsResult:
    return SignalsResult(
        status=status, reason=cast(SignalsReason, reason), remarks=remarks, signals=fields
    )


def _extraction_cache_key(digest: str, fingerprint: str) -> str:
    """The key an extraction is cached under: the call's input hash and the injection scan it was
    verified against. Two remarks that differ only in angle brackets send the same text but scan
    differently, so they are two entries and never evict each other."""
    return hashlib.sha256(f"{digest}:{fingerprint}".encode()).hexdigest()


def _still_verifies(cached: CachedExtraction, extraction_input: ExtractionInput) -> bool:
    """Whether today's verifier still accepts every stored quote against the text it would send.
    The cache key names the prompt and its inputs, not the code that checks the answer, so a
    verifier tightened since the entry was written must be able to refuse it."""
    again = verify_extraction(
        SignalExtraction(
            signals=[
                SignalClaim(code=cast(SignalCode, signal.code), quote=signal.quote or "")
                for signal in cached.signals
            ],
            injection_suspected=cached.model_flagged_injection,
        ),
        extraction_input,
    )
    return not again.dropped and len(again.signals) == len(cached.signals)


def _cached_extraction(
    engine: Engine, tier: str, key: str, extraction_input: ExtractionInput
) -> CachedExtraction | None:
    """The cached extraction for these inputs when it is still valid and still verifies; else
    None, and the caller makes the call again."""
    with engine.connect() as connection:
        found = llm_store.cached_result(
            connection, SIGNALS_PROMPT_ID, SIGNALS_PROMPT_VERSION, tier, key
        )
    if found is None:
        return None
    try:
        cached = CachedExtraction.model_validate(found)
    except ValidationError:
        return None  # written under another shape of the model: treat it as absent
    return cached if _still_verifies(cached, extraction_input) else None


def _extracted(
    remarks_info: RemarksInfo,
    extraction: CachedExtraction,
    fields: list[StoredSignal],
    *,
    tier: str,
    digest: str,
    call_id: int | None,
) -> SignalsResult:
    return SignalsResult(
        status="extracted",
        reason=None,
        remarks=remarks_info,
        signals=[*extraction.signals, *fields],
        dropped=extraction.dropped,
        model_flagged_injection=extraction.model_flagged_injection,
        extraction=ExtractionInfo(
            prompt_id=SIGNALS_PROMPT_ID,
            prompt_version=SIGNALS_PROMPT_VERSION,
            tier=tier,
            input_sha256=digest,
            reused=call_id is None,
            llm_call_id=call_id,
        ),
    )


def _extract_signals(
    ctx: StageContext,
    client: MeteredClient,
    blocked: Blocked,
    candidate_id: int,
    remarks: str,
    fields: list[StoredSignal],
) -> SignalsResult:
    """The candidate's remarks read for signals: from the cache, else one call. Raises what the
    call raises."""
    extraction_input = build_extraction_input(remarks)
    info = _remarks_info(remarks, extraction_input)
    inputs = extraction_inputs(extraction_input)
    tier = client.tier_for(SIGNALS_TASK).value
    digest = input_sha256(EXTRACT_PROMPT, client.tier_for(SIGNALS_TASK), inputs)
    fingerprint = _scan_fingerprint(extraction_input.hits)

    cache_key = _extraction_cache_key(digest, fingerprint)
    cached = _cached_extraction(ctx.engine, tier, cache_key, extraction_input)
    if cached is not None:
        return _extracted(info, cached, fields, tier=tier, digest=digest, call_id=None)
    if blocked.reason is not None:
        return _without_extraction("deferred", blocked.reason, fields, info)

    response = client.call_sync(
        EXTRACT_PROMPT,
        inputs=inputs,
        output=SignalExtraction,
        stage=SIGNALS_STAGE,
        task=SIGNALS_TASK,
        candidate_id=candidate_id,
    )
    call_id = client.last_call_id
    verified = verify_extraction(response.output, extraction_input)
    extraction = CachedExtraction(
        signals=[StoredSignal.from_verified(signal) for signal in verified.signals],
        dropped=verified.dropped,
        model_flagged_injection=verified.model_flagged_injection,
        scan_fingerprint=fingerprint,
    )
    with ctx.engine.begin() as connection:
        llm_store.cache_result(
            connection,
            SIGNALS_PROMPT_ID,
            SIGNALS_PROMPT_VERSION,
            tier,
            cache_key,
            extraction.model_dump(mode="json"),
            call_id,
        )
    return _extracted(info, extraction, fields, tier=tier, digest=digest, call_id=call_id)


def _signals_for(
    ctx: StageContext, blocked: Blocked, source: llm_store.SignalSource, threshold_days: int
) -> SignalsResult:
    fields = [
        StoredSignal.from_field(found)
        for found in field_signals(
            FieldSignalInput(
                change_kind=source.change_kind,
                price=source.price,
                prev_price=source.prev_price,
                listed_date=source.listed_date,
                as_of=ctx.run_date,
            ),
            threshold_days,
        )
    ]
    if not source.remarks:
        return _without_extraction("fields_only", "no_remarks", fields)
    if ctx.client is None:
        info = _remarks_info(source.remarks, build_extraction_input(source.remarks))
        return _without_extraction("fields_only", "llm_not_configured", fields, info)
    try:
        return _extract_signals(
            ctx, ctx.client, blocked, source.candidate_id, source.remarks, fields
        )
    except Exception as error:
        failure = classify(error)
        if failure is None:
            raise
        info = _remarks_info(source.remarks, build_extraction_input(source.remarks))
        match failure.action:
            case "permanent":
                raise _permanent(SIGNALS_STAGE, source.candidate_id, error) from None
            case "budget":
                blocked.reason = "budget"
                return _without_extraction("deferred", "budget", fields, info)
            case "provider":
                blocked.reason = "provider_error"
                blocked.provider_failed = True
                return _without_extraction("failed", "provider_error", fields, info)
            case "candidate":
                return _without_extraction("failed", failure.reason, fields, info)
            case _ as unreachable:
                assert_never(unreachable)


def _write_signals(
    ctx: StageContext, source: llm_store.SignalSource, result: SignalsResult
) -> bool:
    def write(connection: Connection) -> None:
        llm_store.write_signals(
            connection, ctx.run_id, source.candidate_id, source.listing_id, result
        )

    return _write(ctx, source.candidate_id, write)


def run_signals(ctx: StageContext, pack: MarketPack) -> dict[str, Any]:
    """Stage 6: field signals for every ranked candidate and, where it has remarks and a model is
    configured, the remarks read for signals. One row per candidate, committed as it is made.

    The counts are merged into the run also when the stage fails part way, and returned."""
    tally = SignalTally()
    blocked = ctx.blocked
    try:
        with _one_spender(ctx.engine, ctx.market):
            with ctx.engine.connect() as connection:
                sources = llm_store.signal_sources(connection, ctx.run_id)
            for source in sources:
                result = _signals_for(ctx, blocked, source, pack.signals.long_on_market_days)
                if not _write_signals(ctx, source, result):
                    raise _superseded(SIGNALS_STAGE)
                tally.add(result)
        if blocked.provider_failed:
            raise _retryable(SIGNALS_STAGE)
    finally:
        counts = _merge_counts(ctx, asdict(tally))
    return counts


# --- stage 7: narratives ------------------------------------------------------------------------


@dataclass
class NarrativeTally:
    narratives_accepted: int = 0
    narratives_rejected: int = 0
    narratives_failed: int = 0
    narratives_deferred: int = 0
    narratives_not_eligible: int = 0
    narratives_reused: int = 0
    # Accepted narratives whose first draft failed the check and whose repair passed.
    narratives_repaired: int = 0

    def add(self, result: NarrativeResult) -> None:
        match result.status:
            case "accepted":
                self.narratives_accepted += 1
                reused = result.model is not None and result.model.reused
                if not reused and result.check is not None and result.check.attempts == 2:
                    self.narratives_repaired += 1
            case "rejected":
                self.narratives_rejected += 1
            case "failed":
                self.narratives_failed += 1
            case "deferred":
                self.narratives_deferred += 1
            case "not_eligible":
                self.narratives_not_eligible += 1
        if result.model is not None and result.model.reused:
            self.narratives_reused += 1


def _unwritten(status: Literal["failed", "deferred"], reason: str, facts: Facts) -> NarrativeResult:
    """A narrative that was not written: the facts it would have been written from, no text."""
    return NarrativeResult(
        status=status, reason=cast(NarrativeReason, reason), facts=StoredFacts(**facts.stored())
    )


def _not_eligible(proforma_status: str) -> NarrativeResult:
    reason = "proforma_no_arv" if proforma_status == "no_arv" else "proforma_unsizable"
    return NarrativeResult(
        status="not_eligible",
        reason=cast(NarrativeReason, reason),
        facts=StoredFacts(figures={}, codes=[]),
    )


def _reused(cached: NarrativeResult) -> NarrativeResult:
    """A cached result for this run: marked as reused and naming no call, as none was made."""
    if cached.model is None:
        raise ValueError("a cached narrative has its model record")
    return cached.model_copy(
        update={"model": cached.model.model_copy(update={"reused": True, "llm_call_ids": []})}
    )


def _cached_narrative(
    engine: Engine, tier: str, digest: str, facts: Facts
) -> NarrativeResult | None:
    """The cached narrative for these inputs when it is still valid and, if it was accepted, still
    passes today's figure check against the facts; else None, and the caller writes it again. A
    rejected one stays rejected: its text was not kept to check."""
    with engine.connect() as connection:
        found = llm_store.cached_result(
            connection, NARRATIVE_PROMPT_ID, NARRATIVE_PROMPT_VERSION, tier, digest
        )
    if found is None:
        return None
    try:
        cached = NarrativeResult.model_validate(found)
    except ValidationError:
        return None  # written under another shape of the model: treat it as absent
    if cached.status == "accepted" and not check_narrative(_draft_of(cached), facts).passed:
        return None
    return _reused(cached)


def _draft_of(accepted: NarrativeResult) -> NarrativeDraft:
    """The draft an accepted result was made from, to check it again."""
    return NarrativeDraft(
        summary=accepted.summary or "",
        risks=[RiskPoint(basis=list(risk.basis), text=risk.text) for risk in accepted.risks],
        checks_before_offer=list(accepted.checks_before_offer),
    )


def _write_new_narrative(
    ctx: StageContext,
    client: MeteredClient,
    candidate_id: int,
    facts: Facts,
    tier: str,
    digest: str,
) -> NarrativeResult:
    """One narrative written by the model, checked, and cached. Raises what the call raises."""
    attempt = write_narrative_sync(client, facts, NARRATIVE_STAGE, candidate_id)
    result = (
        accepted_result(attempt, facts)
        if attempt.draft is not None
        else rejected_result(attempt, facts)
    )
    with ctx.engine.begin() as connection:
        llm_store.cache_result(
            connection,
            NARRATIVE_PROMPT_ID,
            NARRATIVE_PROMPT_VERSION,
            tier,
            digest,
            result.model_dump(mode="json"),
            attempt.llm_call_ids[0] if attempt.llm_call_ids else None,
        )
    return result


def _narrative_for(
    ctx: StageContext,
    blocked: Blocked,
    source: llm_store.NarrativeSource,
    signals: SignalsResult | None,
) -> tuple[NarrativeResult, str | None]:
    """The candidate's narrative and the hash of its inputs (None when it has no inputs)."""
    if source.proforma_status != "computed":
        return _not_eligible(source.proforma_status), None
    facts = build_facts(ProformaResult.model_validate(source.result), signals)
    if ctx.client is None:
        return _unwritten("deferred", "llm_not_configured", facts), None
    client = ctx.client
    tier = client.tier_for(NARRATIVE_TASK)
    digest = input_sha256(NARRATIVE_PROMPT, tier, narrative_inputs(facts))

    cached = _cached_narrative(ctx.engine, tier.value, digest, facts)
    if cached is not None:
        return cached, digest
    if blocked.reason is not None:
        return _unwritten("deferred", blocked.reason, facts), digest
    try:
        return (
            _write_new_narrative(ctx, client, source.candidate_id, facts, tier.value, digest),
            digest,
        )
    except Exception as error:
        failure = classify(error)
        if failure is None:
            raise
        match failure.action:
            case "permanent":
                raise _permanent(NARRATIVE_STAGE, source.candidate_id, error) from None
            case "budget":
                blocked.reason = "budget"
                return _unwritten("deferred", "budget", facts), digest
            case "provider":
                blocked.reason = "provider_error"
                blocked.provider_failed = True
                return _unwritten("failed", "provider_error", facts), digest
            case "candidate":
                return _unwritten("failed", failure.reason, facts), digest
            case _ as unreachable:
                assert_never(unreachable)


def _write_narrative(
    ctx: StageContext,
    source: llm_store.NarrativeSource,
    digest: str | None,
    result: NarrativeResult,
) -> bool:
    def write(connection: Connection) -> None:
        llm_store.write_narrative(connection, ctx.run_id, source.candidate_id, digest, result)

    return _write(ctx, source.candidate_id, write)


def run_narratives(ctx: StageContext) -> dict[str, Any]:
    """Stage 7: a narrative for every ranked candidate whose pro-forma was computed, and a
    `not_eligible` row for the others. One row per candidate, committed as it is made.

    The counts are merged into the run also when the stage fails part way, and returned."""
    tally = NarrativeTally()
    blocked = ctx.blocked
    try:
        with _one_spender(ctx.engine, ctx.market):
            with ctx.engine.connect() as connection:
                sources = llm_store.narrative_sources(connection, ctx.run_id)
                signals = llm_store.signals_results(connection, ctx.run_id)
            for source in sources:
                result, digest = _narrative_for(
                    ctx, blocked, source, signals.get(source.candidate_id)
                )
                if not _write_narrative(ctx, source, digest, result):
                    raise _superseded(NARRATIVE_STAGE)
                tally.add(result)
        if blocked.provider_failed:
            raise _retryable(NARRATIVE_STAGE)
    finally:
        counts = _merge_counts(ctx, asdict(tally))
    return counts

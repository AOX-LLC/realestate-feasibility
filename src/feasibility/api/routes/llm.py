"""Read-only views of what the model stages stored, and of what they cost.

This module imports nothing that can call a model, spend or write: no client, no metered client,
no stage, no ledger writer. It imports the read functions of `llm/store.py` and the shapes of the
stored results. A rejected narrative is served as its status, its reason and the kinds of rule it
broke, never the model's text and never the text of a violation.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status

from feasibility.api.deps import DEFAULT_PAGE_SIZE, AfterId, EngineDep, Limit, SettingsDep
from feasibility.api.routes.sourcing import CandidateId, RunId, require_run
from feasibility.api.schemas import (
    CandidateLlmOut,
    CostLineOut,
    DroppedClaimOut,
    ExtractionOut,
    MonthSpendOut,
    NarrativeCheckOut,
    NarrativeFactsOut,
    NarrativeLineOut,
    NarrativeModelOut,
    NarrativeOut,
    Page,
    QuotedFigureOut,
    RemarksReadOut,
    RiskPointOut,
    RunCostOut,
    SignalOut,
    SignalsOut,
)
from feasibility.llm.ledger import month_bounds
from feasibility.llm.narrative import NarrativeResult
from feasibility.llm.results import SignalsResult
from feasibility.llm.store import (
    CostLine,
    candidate_in_run,
    month_spend,
    narrative_lines,
    read_narrative,
    read_signals,
    run_cost,
)

router = APIRouter(tags=["model stages"])

NarrativeStatus = Literal["accepted", "rejected", "failed", "deferred", "not_eligible"]
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


def _signals_out(result: SignalsResult) -> SignalsOut:
    remarks = result.remarks
    extraction = result.extraction
    return SignalsOut(
        status=result.status,
        reason=result.reason,
        remarks=None
        if remarks is None
        else RemarksReadOut(
            char_count=remarks.char_count,
            redaction_count=remarks.redaction_count,
            removed_invisible_count=remarks.removed_invisible_count,
            suspicious=remarks.suspicious,
            suspicious_rules=list(remarks.suspicious_rules),
        ),
        signals=[
            SignalOut(
                code=signal.code,
                polarity=signal.polarity,
                source=signal.source,
                quote=signal.quote,
                field=signal.field,
                field_value=signal.field_value,
            )
            for signal in result.signals
        ],
        dropped=[DroppedClaimOut(code=claim.code, reason=claim.reason) for claim in result.dropped],
        model_flagged_injection=result.model_flagged_injection,
        extraction=None
        if extraction is None
        else ExtractionOut(
            prompt_id=extraction.prompt_id,
            prompt_version=extraction.prompt_version,
            tier=extraction.tier,
            reused=extraction.reused,
            llm_call_id=extraction.llm_call_id,
        ),
    )


def _narrative_out(result: NarrativeResult) -> NarrativeOut:
    """Text only from an accepted narrative: every other status is served without any, whatever
    the stored result holds."""
    accepted = result.status == "accepted"
    check = result.check
    model = result.model
    return NarrativeOut(
        status=result.status,
        reason=result.reason,
        summary=result.summary if accepted else None,
        risks=[RiskPointOut(basis=list(r.basis), text=r.text) for r in result.risks]
        if accepted
        else [],
        checks_before_offer=list(result.checks_before_offer) if accepted else [],
        figures_quoted=[QuotedFigureOut(key=f.key, text=f.text) for f in result.figures_quoted]
        if accepted
        else [],
        check=None
        if check is None
        else NarrativeCheckOut(
            passed=check.passed,
            attempts=check.attempts,
            violation_kinds=[violation.kind for violation in check.violations],
        ),
        facts=NarrativeFactsOut(figures=dict(result.facts.figures), codes=list(result.facts.codes)),
        model=None
        if model is None
        else NarrativeModelOut(
            prompt_id=model.prompt_id,
            prompt_version=model.prompt_version,
            tier=model.tier,
            reused=model.reused,
            llm_call_ids=list(model.llm_call_ids),
        ),
    )


def _cost_line(line: CostLine) -> CostLineOut:
    return CostLineOut(
        key=line.key,
        calls=line.calls,
        refused_calls=line.refused,
        input_tokens=line.input_tokens,
        output_tokens=line.output_tokens,
        cost_usd=line.cost_usd,
        reserved_unknown_usd=line.reserved_unknown_usd,
    )


@router.get("/sourcing/runs/{run_id}/candidates/{candidate_id}/llm")
def get_candidate_llm(
    engine: EngineDep, run_id: RunId, candidate_id: CandidateId
) -> CandidateLlmOut:
    """One candidate's signals and risk narrative in one run. Either is null when its stage has
    not written a row for the candidate (the run predates the stage, or the stage stopped
    before it)."""
    with engine.connect() as connection:
        require_run(connection, run_id)
        if not candidate_in_run(connection, run_id, candidate_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such candidate in this run")
        signals = read_signals(connection, run_id, candidate_id)
        narrative = read_narrative(connection, run_id, candidate_id)
    return CandidateLlmOut(
        run_id=run_id,
        candidate_id=candidate_id,
        signals=None if signals is None else _signals_out(signals),
        narrative=None if narrative is None else _narrative_out(narrative),
    )


@router.get("/sourcing/runs/{run_id}/narratives")
def list_run_narratives(
    engine: EngineDep,
    run_id: RunId,
    narrative_status: Annotated[NarrativeStatus | None, Query(alias="status")] = None,
    after: AfterId = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[NarrativeLineOut]:
    """A run's narratives in candidate-rank order, `after` being a rank. Optionally only one
    status. The summary is present for an accepted narrative only."""
    with engine.connect() as connection:
        require_run(connection, run_id)
        lines = narrative_lines(
            connection, run_id, status=narrative_status, after_rank=after, limit=limit + 1
        )
    items = [
        NarrativeLineOut(
            candidate_id=line.candidate_id,
            rank=line.rank,
            status=line.status,  # type: ignore[arg-type]
            reason=line.reason,
            summary=line.summary,
        )
        for line in lines[:limit]
    ]
    next_after = str(items[-1].rank) if len(lines) > limit else None
    return Page[NarrativeLineOut](items=items, next_after=next_after)


@router.get("/sourcing/runs/{run_id}/llm/cost")
def get_run_llm_cost(engine: EngineDep, settings: SettingsDep, run_id: RunId) -> RunCostOut:
    """What a run's model calls came to, across every attempt of the run: totals, by stage and
    by model, the reservation held for calls that raised, and the run's budget."""
    with engine.connect() as connection:
        require_run(connection, run_id)
        cost = run_cost(connection, run_id)
    spent = cost.total.cost_usd + cost.total.reserved_unknown_usd
    budget = settings.llm_run_budget_usd
    return RunCostOut(
        run_id=run_id,
        run_budget_usd=budget,
        spent_usd=spent,
        remaining_usd=max(Decimal(0), budget - spent),
        modes=cost.modes,
        total=_cost_line(cost.total),
        by_stage=[_cost_line(line) for line in cost.by_stage],
        by_model=[_cost_line(line) for line in cost.by_model],
    )


@router.get("/llm/spend")
def get_llm_spend(
    engine: EngineDep,
    settings: SettingsDep,
    month: Annotated[str | None, Query(pattern=MONTH)] = None,
) -> MonthSpendOut:
    """Billable calls (record and live) in one UTC month against the monthly budget; the current
    month by default."""
    moment = (
        datetime.now(UTC)
        if month is None
        else datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
    )
    start, end = month_bounds(moment)
    with engine.connect() as connection:
        spend = month_spend(connection, start, end)
    spent = spend.cost_usd + spend.reserved_unknown_usd
    budget = settings.llm_monthly_budget_usd
    return MonthSpendOut(
        month=f"{start.year:04d}-{start.month:02d}",
        billable_calls=spend.calls,
        refused_calls=spend.refused,
        cost_usd=spend.cost_usd,
        reserved_unknown_usd=spend.reserved_unknown_usd,
        spent_usd=spent,
        monthly_budget_usd=budget,
        remaining_usd=max(Decimal(0), budget - spent),
    )

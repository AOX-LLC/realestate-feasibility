"""Building the brief of one run from what the run stored."""

from sqlalchemy import Connection

from feasibility.config import DataMode
from feasibility.delivery import store
from feasibility.delivery.brief import (
    MAX_CANDIDATES,
    Brief,
    BriefCandidate,
    BriefCode,
    NotShown,
    comps_of,
    figures_of,
    narrative_of,
    signals_of,
)
from feasibility.llm.facts import build_facts
from feasibility.proforma.model import ProformaResult


class BriefError(RuntimeError):
    """A brief cannot be built. A retry changes nothing, so a job that hits one is permanent."""


class RunNotFoundError(BriefError):
    pass


class BriefNotReadyError(BriefError):
    """The run has not finished: its rows are still being written."""


def build_brief(connection: Connection, run_id: int, data_mode: DataMode) -> Brief:
    """The run's brief: its computed pro-formas in rank order (at most ten), each with its
    signals and its narrative if the narrative still passes the figure check.

    A run whose later stages failed (`error` set) still has a ranking and pro-formas, so its
    brief is delivered as `partial`; only that a stage failed is said, never the error."""
    header = store.read_run(connection, run_id)
    if header is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    if header.status != "completed":
        raise BriefNotReadyError(f"run {run_id} is {header.status}, not completed")
    counts = store.proforma_status_counts(connection, run_id)
    rows = store.top_computed(connection, run_id, MAX_CANDIDATES)
    ids = [row.candidate_id for row in rows]
    signals = store.signals_for(connection, run_id, ids)
    narratives = store.narratives_for(connection, run_id, ids)

    candidates = []
    for row in rows:
        result = ProformaResult.model_validate(row.result)
        stored_signals = signals.get(row.candidate_id)
        facts = build_facts(result, stored_signals)
        candidates.append(
            BriefCandidate(
                candidate_id=row.candidate_id,
                rank=row.rank,
                score=format(row.score, "f"),
                street=row.street,
                zip5=row.zip5,
                list_price=format(row.list_price, "f"),
                figures=figures_of(result),
                verdict=[fact.code for fact in facts.code_facts],
                comps=comps_of(result),
                flags=[BriefCode(code=flag.code, meaning=flag.meaning) for flag in facts.flags],
                signals=signals_of(stored_signals),
                narrative=narrative_of(narratives.get(row.candidate_id), result, stored_signals),
            )
        )
    computed = counts.get("computed", 0)
    partial = header.error is not None
    return Brief(
        market=header.market,
        as_of=header.as_of,
        run_id=run_id,
        data_mode=data_mode.value,
        completeness="partial" if partial else "complete",
        notice="later_stage_failed" if partial else None,
        ranked=store.ranked_count(connection, run_id),
        shown=len(candidates),
        not_shown=NotShown(
            no_arv=counts.get("no_arv", 0),
            unsizable=counts.get("unsizable", 0),
            over_the_cap=max(0, computed - len(candidates)),
        ),
        candidates=candidates,
    )

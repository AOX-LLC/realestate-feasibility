"""Building the brief of one run from what the run stored."""

import re
from decimal import Decimal

from sqlalchemy import Connection, Engine

from feasibility.delivery import store
from feasibility.delivery.brief import (
    FLAG_CODE,
    MAX_CANDIDATES,
    Brief,
    BriefCandidate,
    BriefCode,
    NotShown,
    TextContext,
    comps_of,
    figures_of,
    narrative_of,
    signals_of,
    street_names_of,
)
from feasibility.domain.address import normalize_street
from feasibility.llm.facts import build_facts
from feasibility.proforma.model import ProformaResult


class BriefError(RuntimeError):
    """A brief cannot be built. A retry changes nothing, so a job that hits one is permanent."""


class RunNotFoundError(BriefError):
    pass


class BriefNotReadyError(BriefError):
    """The run has not finished: its rows are still being written."""


def build_brief(connection: Connection, run_id: int) -> Brief:
    """The run's brief: its computed pro-formas in rank order (at most ten), each with its
    signals and its narrative if the narrative still passes the figure check.

    A run whose later stages failed (`error` set) still has a ranking and pro-formas, so its
    brief is delivered as `partial`; only that a stage failed is said, never the error."""
    header = store.read_run(connection, run_id)
    if header is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    if header.status != "completed":
        raise BriefNotReadyError(f"run {run_id} is {header.status}, not completed")
    if not header.stages_finished:
        # The ranking is stored and the run completed before the estimates, pro-formas, signals
        # and narratives are: a brief built now would call every narrative "not available".
        raise BriefNotReadyError(f"run {run_id} has stages that have not ended")
    counts = store.proforma_status_counts(connection, run_id)
    rows = store.top_computed(connection, run_id, MAX_CANDIDATES)
    ids = [row.candidate_id for row in rows]
    signals, unreadable = store.signals_for(connection, run_id, ids)
    narratives = store.narratives_for(connection, run_id, ids)
    remarks = store.remarks_of(connection, run_id, ids)

    results = [ProformaResult.model_validate(row.result) for row in rows]
    # What no narrative of this brief may name: the streets of every candidate shown and of the
    # sales their values rest on (an address is not for a model to repeat).
    addresses = [row.street for row in rows]
    addresses += [line.address for result in results if result.arv for line in result.arv.comps]
    context = TextContext(remarks=remarks, street_names=street_names_of(addresses))

    for row, result in zip(rows, results, strict=True):
        _check_against_columns(row, result)

    candidates = []
    for row, result in zip(rows, results, strict=True):
        stored_signals = signals.get(row.candidate_id)
        facts = build_facts(result, stored_signals)
        candidates.append(
            BriefCandidate(
                candidate_id=row.candidate_id,
                rank=row.rank,
                score=format(row.score, "f"),
                street=_street(row.street),
                zip5=row.zip5 if row.zip5 and re.fullmatch(r"[0-9]{5}", row.zip5) else None,
                list_price=format(row.list_price, "f"),
                figures=figures_of(result),
                verdict=[fact.code for fact in facts.code_facts],
                comps=comps_of(result),
                flags=[
                    BriefCode(code=flag.code, meaning=flag.meaning)
                    for flag in facts.flags
                    if re.fullmatch(FLAG_CODE, flag.code)
                ],
                signals=signals_of(stored_signals),
                narrative=narrative_of(
                    narratives.get(row.candidate_id),
                    result,
                    stored_signals,
                    context,
                    signals_unreadable=row.candidate_id in unreadable,
                ),
            )
        )
    computed = counts.get("computed", 0)
    ranked = store.ranked_count(connection, run_id)
    partial = header.error is not None
    return Brief(
        market=header.market,
        as_of=header.as_of,
        run_id=run_id,
        data_mode=header.data_mode,  # type: ignore[arg-type]
        completeness="partial" if partial else "complete",
        notice="later_stage_failed" if partial else None,
        ranked=ranked,
        shown=len(candidates),
        not_shown=NotShown(
            no_arv=counts.get("no_arv", 0),
            unsizable=counts.get("unsizable", 0),
            over_the_cap=max(0, computed - len(candidates)),
            no_pro_forma=max(0, ranked - sum(counts.values())),
        ),
        candidates=candidates,
    )


def _street(raw: str) -> str:
    """The street as the brief carries it: normalised again, at most 120 characters. A street
    that normalises to nothing (all non-ASCII) is shown as unknown rather than losing the brief."""
    return normalize_street(raw)[:120].strip() or "ADDRESS UNKNOWN"


def _check_against_columns(row: store.ComputedRow, result: ProformaResult) -> None:
    """The stored result and the columns beside it come from one pro-forma; if they disagree,
    one was edited or written by a bug, and which one to trust is not for a brief to guess."""
    figures = figures_of(result)
    pairs = {
        "offer_price": (row.list_price, figures.offer_price),
        "arv": (row.arv, figures.arv),
        "total_cost": (row.total_cost, figures.total_cost),
        "profit": (row.profit, figures.profit),
        "margin": (row.margin, figures.margin),
        "max_offer": (row.max_offer, figures.max_offer),
        "roi": (row.roi, figures.roi),
        "annualized_return": (row.annualized_return, figures.annualized_return),
    }
    for name, (column, shown) in pairs.items():
        same = (column is None and shown is None) or (
            column is not None and shown is not None and column == Decimal(shown)
        )
        if not same:
            raise BriefError(
                f"the stored pro-forma of candidate {row.candidate_id} disagrees "
                f"with its own {name} column"
            )
    arv = result.arv
    if list(result.flags) != row.flags:
        raise BriefError(
            f"the stored pro-forma of candidate {row.candidate_id} disagrees with its flags"
        )
    if arv is not None and arv.estimate_fetched_on != row.estimate_fetched_on:
        raise BriefError(
            f"the stored pro-forma of candidate {row.candidate_id} disagrees with its estimate date"
        )
    if arv is not None and arv.comp_count_used != sum(1 for line in arv.comps if line.used):
        raise BriefError(
            f"the stored pro-forma of candidate {row.candidate_id} counts comps it does not hold"
        )


def build_brief_snapshot(engine: Engine, run_id: int) -> Brief:
    """The brief, read from one snapshot of the database: a re-run that commits halfway through
    the reads cannot give it a pro-forma from one attempt and a narrative from another."""
    repeatable = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    with repeatable as connection, connection.begin():
        return build_brief(connection, run_id)


def build_and_store(engine: Engine, run_id: int) -> tuple[Brief, str]:
    """Build the run's brief from one snapshot and keep it. Returns the brief and its hash."""
    repeatable = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    with repeatable as connection, connection.begin():
        brief = build_brief(connection, run_id)
        return brief, store.write_brief(connection, brief)

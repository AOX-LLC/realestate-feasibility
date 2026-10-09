"""All the SQL of the model stages' results: the input-hash cache, each run's signals and
narratives, and the reads of the ledger that the API and the command line show.

Everything stored here is a verified result, never the model's raw answer. A rejected narrative
keeps its violations and no text (see `NarrativeResult`). The ledger itself is written by
`ledger.py`; this module only reads it, in sums.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, func, literal, select
from sqlalchemy.dialects.postgresql import insert

from feasibility.llm.narrative import NarrativeResult
from feasibility.llm.results import SignalsResult
from feasibility.tables import (
    candidate_narrative,
    candidate_signals,
    listing,
    llm_call,
    llm_result,
    proforma,
    run_candidate,
    run_listing,
    sourcing_run,
)

ZERO = Decimal(0)
REFUSED = "budget_refused"


# --- the cache ----------------------------------------------------------------------------------


def cached_result(
    connection: Connection, prompt_id: str, prompt_version: int, tier: str, input_sha256: str
) -> dict[str, Any] | None:
    """The verified result stored for these inputs, or None."""
    found: dict[str, Any] | None = connection.execute(
        select(llm_result.c.result).where(
            llm_result.c.prompt_id == prompt_id,
            llm_result.c.prompt_version == prompt_version,
            llm_result.c.tier == tier,
            llm_result.c.input_sha256 == input_sha256,
        )
    ).scalar_one_or_none()
    return found


def cache_result(
    connection: Connection,
    prompt_id: str,
    prompt_version: int,
    tier: str,
    input_sha256: str,
    result: dict[str, Any],
    llm_call_id: int | None,
) -> None:
    """Store a verified result under its inputs, replacing an older one for the same inputs."""
    statement = insert(llm_result).values(
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        tier=tier,
        input_sha256=input_sha256,
        result=result,
        llm_call_id=llm_call_id,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[
                llm_result.c.prompt_id,
                llm_result.c.prompt_version,
                llm_result.c.tier,
                llm_result.c.input_sha256,
            ],
            set_={
                "result": statement.excluded.result,
                "llm_call_id": statement.excluded.llm_call_id,
                "created_at": func.now(),
            },
        )
    )


# --- what the stages read ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalSource:
    """A ranked candidate as stage 6 reads it: its primary listing's remarks and the run's diff
    row for that listing."""

    candidate_id: int
    rank: int
    listing_id: int
    remarks: str | None
    listed_date: date | None
    change_kind: str
    price: Decimal | None
    prev_price: Decimal | None


def signal_sources(connection: Connection, run_id: int) -> list[SignalSource]:
    """The run's ranked candidates in rank order, one set-based read."""
    rows = connection.execute(
        select(
            run_candidate.c.candidate_id,
            run_candidate.c.rank,
            listing.c.id.label("listing_id"),
            listing.c.remarks,
            listing.c.listed_date,
            run_listing.c.change_kind,
            run_listing.c.price,
            run_listing.c.prev_price,
        )
        .select_from(
            run_candidate.join(listing, listing.c.id == run_candidate.c.primary_listing_id).join(
                run_listing,
                (run_listing.c.run_id == run_candidate.c.run_id)
                & (run_listing.c.listing_id == run_candidate.c.primary_listing_id),
            )
        )
        .where(run_candidate.c.run_id == run_id, run_candidate.c.status == "ranked")
        .order_by(run_candidate.c.rank)
    )
    return [SignalSource(**row._mapping) for row in rows]


@dataclass(frozen=True, slots=True)
class NarrativeSource:
    """A ranked candidate's stored pro-forma, as stage 7 reads it."""

    candidate_id: int
    rank: int
    proforma_status: str
    result: dict[str, Any]


def narrative_sources(connection: Connection, run_id: int) -> list[NarrativeSource]:
    """The run's pro-formas in rank order. A ranked candidate with no pro-forma (its price was not
    positive) is not listed: there is nothing to write about."""
    rows = connection.execute(
        select(
            run_candidate.c.candidate_id,
            run_candidate.c.rank,
            proforma.c.status.label("proforma_status"),
            proforma.c.result,
        )
        .select_from(
            run_candidate.join(
                proforma,
                (proforma.c.run_id == run_candidate.c.run_id)
                & (proforma.c.candidate_id == run_candidate.c.candidate_id),
            )
        )
        .where(run_candidate.c.run_id == run_id)
        .order_by(run_candidate.c.rank)
    )
    return [NarrativeSource(**row._mapping) for row in rows]


def signals_results(connection: Connection, run_id: int) -> dict[int, SignalsResult]:
    """Every candidate's stored signals in the run, by candidate id."""
    rows = connection.execute(
        select(candidate_signals.c.candidate_id, candidate_signals.c.result).where(
            candidate_signals.c.run_id == run_id
        )
    )
    return {row.candidate_id: SignalsResult.model_validate(row.result) for row in rows}


def candidate_is_current(connection: Connection, run_id: int, candidate_id: int) -> bool:
    """Whether the run is still completed and still has the candidate. A newer attempt of the
    run resets it and rebuilds its rows; a row for a candidate it no longer holds cannot be
    written."""
    found = connection.execute(
        select(literal(1))
        .select_from(run_candidate.join(sourcing_run, sourcing_run.c.id == run_candidate.c.run_id))
        .where(
            run_candidate.c.run_id == run_id,
            run_candidate.c.candidate_id == candidate_id,
            sourcing_run.c.status == "completed",
        )
    ).first()
    return found is not None


# --- what the stages write ----------------------------------------------------------------------


def write_signals(
    connection: Connection, run_id: int, candidate_id: int, listing_id: int, result: SignalsResult
) -> None:
    """The candidate's signals row for the run (replacing one written earlier by this run)."""
    statement = insert(candidate_signals).values(
        run_id=run_id,
        candidate_id=candidate_id,
        status=result.status,
        reason=result.reason,
        listing_id=listing_id,
        result=result.model_dump(mode="json"),
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[candidate_signals.c.run_id, candidate_signals.c.candidate_id],
            set_={
                "status": statement.excluded.status,
                "reason": statement.excluded.reason,
                "listing_id": statement.excluded.listing_id,
                "result": statement.excluded.result,
            },
        )
    )


def write_narrative(
    connection: Connection,
    run_id: int,
    candidate_id: int,
    input_sha256: str | None,
    result: NarrativeResult,
) -> None:
    """The candidate's narrative row for the run (replacing one written earlier by this run)."""
    statement = insert(candidate_narrative).values(
        run_id=run_id,
        candidate_id=candidate_id,
        status=result.status,
        reason=result.reason,
        input_sha256=input_sha256,
        result=result.model_dump(mode="json"),
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[candidate_narrative.c.run_id, candidate_narrative.c.candidate_id],
            set_={
                "status": statement.excluded.status,
                "reason": statement.excluded.reason,
                "input_sha256": statement.excluded.input_sha256,
                "result": statement.excluded.result,
            },
        )
    )


# --- what the API and the command line read ----------------------------------------------------


def read_signals(connection: Connection, run_id: int, candidate_id: int) -> SignalsResult | None:
    found = connection.execute(
        select(candidate_signals.c.result).where(
            candidate_signals.c.run_id == run_id, candidate_signals.c.candidate_id == candidate_id
        )
    ).scalar_one_or_none()
    return None if found is None else SignalsResult.model_validate(found)


def read_narrative(
    connection: Connection, run_id: int, candidate_id: int
) -> NarrativeResult | None:
    found = connection.execute(
        select(candidate_narrative.c.result).where(
            candidate_narrative.c.run_id == run_id,
            candidate_narrative.c.candidate_id == candidate_id,
        )
    ).scalar_one_or_none()
    return None if found is None else NarrativeResult.model_validate(found)


def candidate_in_run(connection: Connection, run_id: int, candidate_id: int) -> bool:
    return (
        connection.execute(
            select(literal(1)).where(
                run_candidate.c.run_id == run_id, run_candidate.c.candidate_id == candidate_id
            )
        ).first()
        is not None
    )


@dataclass(frozen=True, slots=True)
class NarrativeLine:
    """One line of a run's narrative list. `summary` is present only for an accepted narrative:
    a rejected one has no text to show."""

    candidate_id: int
    rank: int
    status: str
    reason: str | None
    summary: str | None


def narrative_lines(
    connection: Connection,
    run_id: int,
    *,
    status: str | None = None,
    after_rank: int | None = None,
    limit: int,
) -> list[NarrativeLine]:
    """A run's narratives in rank order, `limit` of them after the given rank.

    Ask for one more than the page holds to learn whether another page follows."""
    query = (
        select(
            candidate_narrative.c.candidate_id,
            run_candidate.c.rank,
            candidate_narrative.c.status,
            candidate_narrative.c.reason,
            candidate_narrative.c.result["summary"].astext.label("summary"),
        )
        .select_from(
            candidate_narrative.join(
                run_candidate,
                (run_candidate.c.run_id == candidate_narrative.c.run_id)
                & (run_candidate.c.candidate_id == candidate_narrative.c.candidate_id),
            )
        )
        .where(candidate_narrative.c.run_id == run_id)
    )
    if status is not None:
        query = query.where(candidate_narrative.c.status == status)
    if after_rank is not None:
        query = query.where(run_candidate.c.rank > after_rank)
    rows = connection.execute(query.order_by(run_candidate.c.rank).limit(limit))
    return [
        NarrativeLine(
            row.candidate_id,
            row.rank,
            row.status,
            row.reason,
            # Only an accepted narrative has a summary; anything else is served as no text.
            row.summary if row.status == "accepted" else None,
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class CostLine:
    """The ledger rows of one group. `cost_usd` sums the calls whose cost is known;
    `reserved_unknown_usd` is what was held for calls that raised, whose real cost is not."""

    key: str | None
    calls: int
    refused: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    reserved_unknown_usd: Decimal


@dataclass(frozen=True, slots=True)
class CostBlock:
    total: CostLine
    by_stage: list[CostLine]
    by_model: list[CostLine]
    modes: list[str]


_CALLS = func.count().filter(llm_call.c.outcome != REFUSED)
_REFUSED = func.count().filter(llm_call.c.outcome == REFUSED)
_INPUT = func.coalesce(func.sum(llm_call.c.input_tokens), 0)
_OUTPUT = func.coalesce(func.sum(llm_call.c.output_tokens), 0)
_COST = func.coalesce(func.sum(llm_call.c.cost_usd), 0)
_UNKNOWN = func.coalesce(func.sum(llm_call.c.reserved_usd).filter(llm_call.c.cost_usd.is_(None)), 0)
_MEASURES = (_CALLS, _REFUSED, _INPUT, _OUTPUT, _COST, _UNKNOWN)


def _line(key: str | None, row: Sequence[Any]) -> CostLine:
    calls, refused, input_tokens, output_tokens, cost, unknown = row
    return CostLine(
        key,
        int(calls),
        int(refused),
        int(input_tokens),
        int(output_tokens),
        Decimal(cost),
        Decimal(unknown),
    )


def _grouped(connection: Connection, run_id: int, column: Any) -> list[CostLine]:
    rows = connection.execute(
        select(column, *_MEASURES)
        .where(llm_call.c.run_id == run_id)
        .group_by(column)
        .order_by(column)
    )
    return [_line(row[0], row[1:]) for row in rows]


def run_cost(connection: Connection, run_id: int) -> CostBlock:
    """What a run's model calls came to, across every attempt of the run, in any mode."""
    total = connection.execute(select(*_MEASURES).where(llm_call.c.run_id == run_id)).one()
    modes = connection.execute(
        select(llm_call.c.mode)
        .where(llm_call.c.run_id == run_id)
        .distinct()
        .order_by(llm_call.c.mode)
    ).scalars()
    return CostBlock(
        total=_line(None, total),
        by_stage=_grouped(connection, run_id, llm_call.c.stage),
        by_model=_grouped(connection, run_id, llm_call.c.model),
        modes=list(modes),
    )


@dataclass(frozen=True, slots=True)
class MonthSpend:
    """Billable calls (record and live) in one UTC month, counted as the cap counts them."""

    calls: int
    refused: int
    cost_usd: Decimal
    reserved_unknown_usd: Decimal


def month_spend(connection: Connection, start: datetime, end: datetime) -> MonthSpend:
    row = connection.execute(
        select(_CALLS, _REFUSED, _COST, _UNKNOWN).where(
            llm_call.c.billable, llm_call.c.called_at >= start, llm_call.c.called_at < end
        )
    ).one()
    return MonthSpend(int(row[0]), int(row[1]), Decimal(row[2]), Decimal(row[3]))

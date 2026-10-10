"""All the SQL of the brief: the reads that build one, and the stored copy.

Set-based: a brief costs the same handful of statements for one candidate or ten.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from feasibility.delivery.brief import Brief
from feasibility.llm.narrative import NarrativeResult
from feasibility.llm.results import SignalsResult
from feasibility.tables import (
    brief,
    candidate_narrative,
    candidate_signals,
    listing,
    proforma,
    run_candidate,
    sourcing_run,
)


@dataclass(frozen=True, slots=True)
class RunHeader:
    market: str
    as_of: date
    status: str
    error: str | None
    data_mode: str
    stages_finished: bool


@dataclass(frozen=True, slots=True)
class ComputedRow:
    """A ranked candidate whose pro-forma was computed, with the stored result."""

    candidate_id: int
    rank: int
    score: Decimal
    street: str
    zip5: str | None
    list_price: Decimal
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredBrief:
    content: dict[str, Any]
    content_sha256: str
    built_at: datetime


def read_run(connection: Connection, run_id: int) -> RunHeader | None:
    row = connection.execute(
        select(
            sourcing_run.c.market,
            sourcing_run.c.as_of,
            sourcing_run.c.status,
            sourcing_run.c.error,
            sourcing_run.c.data_mode,
            sourcing_run.c.stages_finished_at.is_not(None).label("stages_finished"),
        ).where(sourcing_run.c.id == run_id)
    ).first()
    if row is None:
        return None
    return RunHeader(
        row.market, row.as_of, row.status, row.error, row.data_mode, row.stages_finished
    )


def ranked_count(connection: Connection, run_id: int) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(run_candidate)
            .where(run_candidate.c.run_id == run_id, run_candidate.c.status == "ranked")
        ).scalar_one()
    )


def proforma_status_counts(connection: Connection, run_id: int) -> dict[str, int]:
    rows = connection.execute(
        select(proforma.c.status, func.count())
        .where(proforma.c.run_id == run_id)
        .group_by(proforma.c.status)
    )
    return {status: int(count) for status, count in rows}


def top_computed(connection: Connection, run_id: int, limit: int) -> list[ComputedRow]:
    """The first `limit` ranked candidates with a computed pro-forma, in rank order."""
    rows = connection.execute(
        select(
            proforma.c.candidate_id,
            run_candidate.c.rank,
            run_candidate.c.score,
            listing.c.address_line,
            listing.c.zip5,
            proforma.c.offer_price,
            proforma.c.result,
        )
        .select_from(
            proforma.join(
                run_candidate,
                (run_candidate.c.run_id == proforma.c.run_id)
                & (run_candidate.c.candidate_id == proforma.c.candidate_id),
            ).join(listing, listing.c.id == run_candidate.c.primary_listing_id)
        )
        .where(proforma.c.run_id == run_id, proforma.c.status == "computed")
        .order_by(run_candidate.c.rank)
        .limit(limit)
    )
    return [
        ComputedRow(
            row.candidate_id,
            row.rank,
            row.score,
            row.address_line,
            row.zip5,
            row.offer_price,
            row.result,
        )
        for row in rows
    ]


def signals_for(
    connection: Connection, run_id: int, candidate_ids: list[int]
) -> dict[int, SignalsResult]:
    """Each candidate's stored signals. A row written under another shape of the model is read
    as absent, which the brief reports as not available."""
    rows = connection.execute(
        select(candidate_signals.c.candidate_id, candidate_signals.c.result).where(
            candidate_signals.c.run_id == run_id,
            candidate_signals.c.candidate_id.in_(candidate_ids),
        )
    )
    found: dict[int, SignalsResult] = {}
    for row in rows:
        try:
            found[row.candidate_id] = SignalsResult.model_validate(row.result)
        except ValidationError:
            continue
    return found


def narratives_for(
    connection: Connection, run_id: int, candidate_ids: list[int]
) -> dict[int, NarrativeResult]:
    rows = connection.execute(
        select(candidate_narrative.c.candidate_id, candidate_narrative.c.result).where(
            candidate_narrative.c.run_id == run_id,
            candidate_narrative.c.candidate_id.in_(candidate_ids),
        )
    )
    found: dict[int, NarrativeResult] = {}
    for row in rows:
        try:
            found[row.candidate_id] = NarrativeResult.model_validate(row.result)
        except ValidationError:
            continue
    return found


def write_brief(connection: Connection, value: Brief) -> str:
    """Store the brief of its run, replacing an earlier one. Returns its content hash."""
    digest = value.content_sha256()
    fields = {
        "version": value.version,
        "completeness": value.completeness,
        "content": value.model_dump(mode="json"),
        "content_sha256": digest,
        "built_at": func.now(),
    }
    statement = insert(brief).values(run_id=value.run_id, **fields)
    connection.execute(
        statement.on_conflict_do_update(index_elements=[brief.c.run_id], set_=fields)
    )
    return digest


def read_brief(connection: Connection, run_id: int) -> StoredBrief | None:
    row = connection.execute(
        select(brief.c.content, brief.c.content_sha256, brief.c.built_at).where(
            brief.c.run_id == run_id
        )
    ).first()
    return None if row is None else StoredBrief(row.content, row.content_sha256, row.built_at)


def remarks_of(connection: Connection, run_id: int, candidate_ids: list[int]) -> list[str]:
    """The stored (redacted) listing text of the candidates' primary listings, for checking that
    a delivered narrative does not repeat it."""
    found = connection.execute(
        select(listing.c.remarks)
        .select_from(
            run_candidate.join(listing, listing.c.id == run_candidate.c.primary_listing_id)
        )
        .where(
            run_candidate.c.run_id == run_id,
            run_candidate.c.candidate_id.in_(candidate_ids),
            listing.c.remarks.is_not(None),
        )
    ).scalars()
    return [text for text in found if text]

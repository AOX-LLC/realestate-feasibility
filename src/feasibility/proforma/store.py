"""All the SQL of stored pro-formas: write a run's, list them, read one."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, delete, insert, select

from feasibility.tables import listing, proforma, run_candidate


@dataclass(frozen=True, slots=True)
class ProformaWrite:
    candidate_id: int
    status: str
    reason: str | None
    estimate_fetched_on: date | None
    offer_price: Decimal
    arv: Decimal | None
    total_cost: Decimal | None
    profit: Decimal | None
    margin: Decimal | None
    roi: Decimal | None
    annualized_return: Decimal | None
    max_offer: Decimal | None
    flags: list[str]
    # The whole ProformaResult as JSON.
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredProforma:
    """One stored pro-forma with the rank and address of its candidate in the run."""

    candidate_id: int
    rank: int
    street: str
    zip5: str | None
    status: str
    reason: str | None
    flags: list[str]
    estimate_fetched_on: date | None
    offer_price: Decimal
    arv: Decimal | None
    total_cost: Decimal | None
    profit: Decimal | None
    margin: Decimal | None
    roi: Decimal | None
    annualized_return: Decimal | None
    max_offer: Decimal | None
    # Only `read_proforma` fills it; a list does not carry every result.
    result: dict[str, Any] | None = None


_SUMMARY_COLUMNS = (
    proforma.c.candidate_id,
    run_candidate.c.rank,
    listing.c.address_line,
    listing.c.zip5,
    proforma.c.status,
    proforma.c.reason,
    proforma.c.flags,
    proforma.c.estimate_fetched_on,
    proforma.c.offer_price,
    proforma.c.arv,
    proforma.c.total_cost,
    proforma.c.profit,
    proforma.c.margin,
    proforma.c.roi,
    proforma.c.annualized_return,
    proforma.c.max_offer,
)


def write_proformas(connection: Connection, run_id: int, rows: Sequence[ProformaWrite]) -> None:
    """Replace the run's pro-formas with these, in the caller's transaction."""
    connection.execute(delete(proforma).where(proforma.c.run_id == run_id))
    if not rows:
        return
    connection.execute(
        insert(proforma),
        [
            {
                "run_id": run_id,
                "candidate_id": row.candidate_id,
                "status": row.status,
                "reason": row.reason,
                "estimate_fetched_on": row.estimate_fetched_on,
                "offer_price": row.offer_price,
                "arv": row.arv,
                "total_cost": row.total_cost,
                "profit": row.profit,
                "margin": row.margin,
                "roi": row.roi,
                "annualized_return": row.annualized_return,
                "max_offer": row.max_offer,
                "flags": row.flags,
                "result": row.result,
            }
            for row in rows
        ],
    )


def _summary_query(run_id: int) -> Any:
    return (
        select(*_SUMMARY_COLUMNS)
        .select_from(
            proforma.join(
                run_candidate,
                (run_candidate.c.run_id == proforma.c.run_id)
                & (run_candidate.c.candidate_id == proforma.c.candidate_id),
            ).join(listing, listing.c.id == run_candidate.c.primary_listing_id)
        )
        .where(proforma.c.run_id == run_id)
    )


def _stored(row: Any, result: dict[str, Any] | None = None) -> StoredProforma:
    return StoredProforma(
        candidate_id=row.candidate_id,
        rank=row.rank,
        street=row.address_line,
        zip5=row.zip5,
        status=row.status,
        reason=row.reason,
        flags=row.flags,
        estimate_fetched_on=row.estimate_fetched_on,
        offer_price=row.offer_price,
        arv=row.arv,
        total_cost=row.total_cost,
        profit=row.profit,
        margin=row.margin,
        roi=row.roi,
        annualized_return=row.annualized_return,
        max_offer=row.max_offer,
        result=result,
    )


def read_proformas(
    connection: Connection,
    run_id: int,
    *,
    status: str | None = None,
    after_rank: int | None = None,
    limit: int,
) -> list[StoredProforma]:
    """A run's pro-formas in candidate-rank order, `limit` of them after the given rank.

    Ask for one more than the page holds to learn whether another page follows."""
    query = _summary_query(run_id)
    if status is not None:
        query = query.where(proforma.c.status == status)
    if after_rank is not None:
        query = query.where(run_candidate.c.rank > after_rank)
    rows = connection.execute(query.order_by(run_candidate.c.rank).limit(limit))
    return [_stored(row) for row in rows]


def read_proforma(connection: Connection, run_id: int, candidate_id: int) -> StoredProforma | None:
    """One candidate's pro-forma in a run, with its full result; None when it has none."""
    row = connection.execute(
        _summary_query(run_id)
        .add_columns(proforma.c.result)
        .where(proforma.c.candidate_id == candidate_id)
    ).first()
    return None if row is None else _stored(row, row.result)

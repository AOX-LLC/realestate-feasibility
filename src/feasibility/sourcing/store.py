"""All the SQL of a sourcing run: matches, runs, candidates and the run's own rows."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from feasibility.sourcing.matching import MatchResult, aggregate
from feasibility.tables import (
    candidate,
    listing,
    listing_match,
    run_candidate,
    run_listing,
    sourcing_run,
)


@dataclass(frozen=True)
class ListingMatch:
    listing_id: int
    result: MatchResult


def write_matches(connection: Connection, rows: Sequence[ListingMatch]) -> None:
    """Record each listing's latest match attempt and point listing.account_id at the
    representative account (NULL unless matched). Writing twice leaves one row per listing."""
    if not rows:
        return
    records = []
    for row in rows:
        matched = aggregate(row.result) if row.result.status == "matched" else None
        records.append(
            {
                "listing_id": row.listing_id,
                "status": row.result.status,
                "method": row.result.method,
                "account_id": matched.account_id if matched else None,
                "gis_parcel_id": matched.gis_parcel_id if matched else None,
                "account_count": row.result.account_count,
                "street_key": row.result.street_key,
            }
        )
    statement = insert(listing_match)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[listing_match.c.listing_id],
            set_={
                "status": statement.excluded.status,
                "method": statement.excluded.method,
                "account_id": statement.excluded.account_id,
                "gis_parcel_id": statement.excluded.gis_parcel_id,
                "account_count": statement.excluded.account_count,
                "street_key": statement.excluded.street_key,
                "matched_at": func.now(),
            },
        ),
        records,
    )
    connection.execute(
        update(listing)
        .where(listing.c.id == bindparam("b_listing_id"))
        .values(account_id=bindparam("b_account_id")),
        [
            {"b_listing_id": record["listing_id"], "b_account_id": record["account_id"]}
            for record in records
        ],
    )


@dataclass(frozen=True, slots=True)
class ListingRow:
    """The listing columns a run reads."""

    id: int
    source: str
    address_line: str
    unit: str | None
    zip5: str | None
    price: Decimal | None
    status: str | None
    property_type: str | None
    lot_size_sqft: Decimal | None
    year_built: int | None
    listed_date: date | None
    first_seen_at: datetime


LISTING_COLUMNS = (
    listing.c.id,
    listing.c.source,
    listing.c.address_line,
    listing.c.unit,
    listing.c.zip5,
    listing.c.price,
    listing.c.status,
    listing.c.property_type,
    listing.c.lot_size_sqft,
    listing.c.year_built,
    listing.c.listed_date,
    listing.c.first_seen_at,
)


@dataclass(frozen=True, slots=True)
class PreviousRunListing:
    """A run_listing row of an earlier run."""

    listing_id: int
    change_kind: str
    price: Decimal | None
    candidate_id: int | None
    filter_reason: str | None
    match_status: str | None
    match_method: str | None
    match_account_id: str | None


@dataclass(frozen=True, slots=True)
class CandidateRef:
    id: int
    property_key: str
    first_as_of: date


@dataclass(frozen=True, slots=True)
class CandidateWrite:
    """A property to get or create. `replaces_key` names an unmatched candidate (an `addr:`
    key) that this property now matches: its row is upgraded in place, keeping its history."""

    property_key: str
    account_id: str | None
    gis_parcel_id: str | None
    zip5: str | None
    street_key: str
    replaces_key: str | None = None


@dataclass(frozen=True, slots=True)
class RunListingWrite:
    listing_id: int
    change_kind: str
    price: Decimal | None
    prev_price: Decimal | None
    # The candidate is named by property key (resolved after the candidates are written) or,
    # for a listing the feed lost, by the id its previous run row carried.
    property_key: str | None
    candidate_id: int | None
    is_primary: bool
    filter_reason: str | None
    # The match this run made; all None when the listing failed a listing-level filter.
    match_status: str | None = None
    match_method: str | None = None
    match_account_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunCandidateWrite:
    candidate_id: int
    primary_listing_id: int
    change_kind: str
    status: str
    filter_reasons: list[str]
    unscored_reason: str | None
    score: Decimal | None
    rank: int | None
    breakdown: dict[str, Any] | None


def lock_market_runs(connection: Connection, market: str) -> None:
    """Serialize runs for one market until the transaction ends."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": f"sourcing_run:{market}"}
    )


def latest_run_as_of(connection: Connection, market: str) -> date | None:
    """The latest date any run was started for, whatever its status. A failed or unfinished
    run may already have moved the listings' last_seen_at, so it counts."""
    latest: date | None = connection.execute(
        select(func.max(sourcing_run.c.as_of)).where(sourcing_run.c.market == market)
    ).scalar_one()
    return latest


def previous_fresh_run(connection: Connection, market: str, before: date) -> int | None:
    """The id of the latest completed run with a fresh sync and an as_of strictly before
    `before`. A stale or skipped run recorded nothing about absence, so a diff against it
    would call every listing it missed relisted."""
    return connection.execute(
        select(sourcing_run.c.id)
        .where(
            sourcing_run.c.market == market,
            sourcing_run.c.status == "completed",
            sourcing_run.c.sync_status == "fresh",
            sourcing_run.c.as_of < before,
        )
        .order_by(sourcing_run.c.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()


def start_run(connection: Connection, market: str, as_of: date) -> int:
    """Create the run row, or reset the existing one for a re-run of the same date."""
    statement = insert(sourcing_run).values(
        market=market, as_of=as_of, status="running", sync_status="pending"
    )
    run_id: int = connection.execute(
        statement.on_conflict_do_update(
            index_elements=[sourcing_run.c.market, sourcing_run.c.as_of],
            set_={
                "status": "running",
                "sync_status": "pending",
                "counts": text("'{}'::jsonb"),
                "error": None,
                "started_at": func.now(),
                "finished_at": None,
            },
        ).returning(sourcing_run.c.id)
    ).scalar_one()
    return run_id


def set_sync_status(connection: Connection, run_id: int, sync_status: str) -> None:
    connection.execute(
        update(sourcing_run).where(sourcing_run.c.id == run_id).values(sync_status=sync_status)
    )


def fail_run(connection: Connection, run_id: int, error: str) -> None:
    connection.execute(
        update(sourcing_run)
        .where(sourcing_run.c.id == run_id)
        .values(status="failed", error=error, finished_at=func.now())
    )


def complete_run(connection: Connection, run_id: int, counts: dict[str, Any]) -> None:
    connection.execute(
        update(sourcing_run)
        .where(sourcing_run.c.id == run_id)
        .values(status="completed", counts=counts, error=None, finished_at=func.now())
    )


def set_run_error(connection: Connection, run_id: int, error: str) -> None:
    """Record that a stage after the build failed. The run stays completed: a completed run
    with an error is ranked, but a later stage did not finish."""
    connection.execute(update(sourcing_run).where(sourcing_run.c.id == run_id).values(error=error))


def merge_counts(connection: Connection, run_id: int, patch: dict[str, Any]) -> None:
    """Add or replace keys of a run's counts, leaving the others as they are."""
    connection.execute(
        update(sourcing_run)
        .where(sourcing_run.c.id == run_id)
        .values(counts=sourcing_run.c.counts.concat(patch))
    )


def listings_seen_between(
    connection: Connection, market: str, start: datetime, end: datetime
) -> list[ListingRow]:
    """Every listing of the market last seen in [start, end), whatever its status."""
    rows = connection.execute(
        select(*LISTING_COLUMNS)
        .where(
            listing.c.market == market,
            listing.c.last_seen_at >= start,
            listing.c.last_seen_at < end,
        )
        .order_by(listing.c.id)
    )
    return [ListingRow(**row._mapping) for row in rows]


def listings_by_id(connection: Connection, listing_ids: Sequence[int]) -> dict[int, ListingRow]:
    if not listing_ids:
        return {}
    rows = connection.execute(select(*LISTING_COLUMNS).where(listing.c.id.in_(list(listing_ids))))
    return {row.id: ListingRow(**row._mapping) for row in rows}


def run_listings_of(connection: Connection, run_id: int) -> list[PreviousRunListing]:
    rows = connection.execute(
        select(
            run_listing.c.listing_id,
            run_listing.c.change_kind,
            run_listing.c.price,
            run_listing.c.candidate_id,
            run_listing.c.filter_reason,
            run_listing.c.match_status,
            run_listing.c.match_method,
            run_listing.c.match_account_id,
        ).where(run_listing.c.run_id == run_id)
    )
    return [PreviousRunListing(**row._mapping) for row in rows]


def candidates_by_key(
    connection: Connection, market: str, property_keys: Collection[str]
) -> dict[str, CandidateRef]:
    if not property_keys:
        return {}
    rows = connection.execute(
        select(candidate.c.id, candidate.c.property_key, candidate.c.first_as_of).where(
            candidate.c.market == market, candidate.c.property_key.in_(list(property_keys))
        )
    )
    return {row.property_key: CandidateRef(**row._mapping) for row in rows}


def upsert_candidates(
    connection: Connection, market: str, as_of: date, writes: Sequence[CandidateWrite]
) -> dict[str, CandidateRef]:
    """Get or create each property's candidate; returns them by property key.

    first_as_of is set on creation only, so a candidate made earlier in this run (or on an
    earlier run date) keeps it.
    """
    for write in writes:
        values = {
            "account_id": write.account_id,
            "gis_parcel_id": write.gis_parcel_id,
            "zip5": write.zip5,
            "street_key": write.street_key,
        }
        if write.replaces_key is not None:
            connection.execute(
                update(candidate)
                .where(candidate.c.market == market, candidate.c.property_key == write.replaces_key)
                .values(property_key=write.property_key, **values)
            )
            continue
        connection.execute(
            insert(candidate)
            .values(market=market, property_key=write.property_key, first_as_of=as_of, **values)
            .on_conflict_do_nothing(index_elements=[candidate.c.market, candidate.c.property_key])
        )
    return candidates_by_key(connection, market, [write.property_key for write in writes])


def clear_run_rows(connection: Connection, run_id: int) -> None:
    connection.execute(delete(run_candidate).where(run_candidate.c.run_id == run_id))
    connection.execute(delete(run_listing).where(run_listing.c.run_id == run_id))


def write_run_listings(
    connection: Connection,
    run_id: int,
    rows: Sequence[RunListingWrite],
    candidates: Mapping[str, CandidateRef],
) -> None:
    if not rows:
        return
    connection.execute(
        insert(run_listing),
        [
            {
                "run_id": run_id,
                "listing_id": row.listing_id,
                "change_kind": row.change_kind,
                "price": row.price,
                "prev_price": row.prev_price,
                "candidate_id": (
                    candidates[row.property_key].id
                    if row.property_key is not None
                    else row.candidate_id
                ),
                "is_primary": row.is_primary,
                "filter_reason": row.filter_reason,
                "match_status": row.match_status,
                "match_method": row.match_method,
                "match_account_id": row.match_account_id,
            }
            for row in rows
        ],
    )


def write_run_candidates(
    connection: Connection, run_id: int, rows: Sequence[RunCandidateWrite]
) -> None:
    if not rows:
        return
    connection.execute(
        insert(run_candidate),
        [
            {
                "run_id": run_id,
                "candidate_id": row.candidate_id,
                "primary_listing_id": row.primary_listing_id,
                "change_kind": row.change_kind,
                "status": row.status,
                "filter_reasons": row.filter_reasons,
                "unscored_reason": row.unscored_reason,
                "score": row.score,
                "rank": row.rank,
                "breakdown": row.breakdown,
            }
            for row in rows
        ],
    )


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """One line of a run's candidate list, for the command line."""

    rank: int | None
    score: Decimal | None
    price: Decimal | None
    change_kind: str
    detail: str
    address: str


def latest_run_id(connection: Connection, market: str) -> int | None:
    run_id: int | None = connection.execute(
        select(sourcing_run.c.id)
        .where(sourcing_run.c.market == market, sourcing_run.c.status == "completed")
        .order_by(sourcing_run.c.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()
    return run_id


def candidate_summaries(
    connection: Connection, run_id: int, status: str, limit: int
) -> list[CandidateSummary]:
    """A run's candidates of one status: ranked ones by rank, the others by candidate id."""
    order = run_candidate.c.rank if status == "ranked" else run_candidate.c.candidate_id
    rows = connection.execute(
        select(
            run_candidate.c.rank,
            run_candidate.c.score,
            run_listing.c.price,
            run_candidate.c.change_kind,
            run_candidate.c.filter_reasons,
            run_candidate.c.unscored_reason,
            listing.c.address_line,
        )
        .select_from(
            run_candidate.join(listing, listing.c.id == run_candidate.c.primary_listing_id).join(
                run_listing,
                (run_listing.c.run_id == run_candidate.c.run_id)
                & (run_listing.c.listing_id == run_candidate.c.primary_listing_id),
            )
        )
        .where(run_candidate.c.run_id == run_id, run_candidate.c.status == status)
        .order_by(order)
        .limit(limit)
    )
    return [
        CandidateSummary(
            rank=row.rank,
            score=row.score,
            price=row.price,
            change_kind=row.change_kind,
            detail=row.unscored_reason or ",".join(row.filter_reasons),
            address=row.address_line,
        )
        for row in rows
    ]

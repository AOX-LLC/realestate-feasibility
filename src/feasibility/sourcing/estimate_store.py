"""All the SQL of value estimates: the stored answer per candidate and date, and the
count of billed estimate calls the monthly cap is measured against."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from feasibility.domain.address import Address
from feasibility.tables import api_request_log, candidate_estimate, listing, run_candidate

# The endpoint literal the RentCast client logs for a value estimate.
VALUE_ESTIMATE_ENDPOINT = "/avm/value"
KEY_COLUMNS = ("candidate_id", "fetched_on")


@dataclass(frozen=True, slots=True)
class EstimateTarget:
    """A ranked candidate worth pricing and the address of its primary listing."""

    candidate_id: int
    rank: int
    address: Address


@dataclass(frozen=True, slots=True)
class EstimateWrite:
    candidate_id: int
    fetched_on: date
    outcome: str
    # The one-line address that was sent.
    address: str
    price: Decimal | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    comp_count: int = 0
    dropped_comp_count: int = 0
    # Kept sale comps, one dict each (address, price, living_area_sqft, distance_miles,
    # year_built, days_old); Decimals as strings.
    comps: Sequence[dict[str, Any]] = ()
    run_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredEstimate:
    candidate_id: int
    fetched_on: date
    outcome: str
    address: str
    price: Decimal | None
    price_low: Decimal | None
    price_high: Decimal | None
    comp_count: int
    dropped_comp_count: int
    comps: list[dict[str, Any]]
    run_id: int | None


def estimate_targets(connection: Connection, run_id: int, top_n: int) -> list[EstimateTarget]:
    """The run's ranked candidates at rank top_n or better, best first, each with the address
    of its primary listing as the run's feed spelled it."""
    rows = connection.execute(
        select(
            run_candidate.c.candidate_id,
            run_candidate.c.rank,
            listing.c.address_line,
            listing.c.unit,
            listing.c.city,
            listing.c.state,
            listing.c.zip5,
        )
        .select_from(
            run_candidate.join(listing, listing.c.id == run_candidate.c.primary_listing_id)
        )
        .where(
            run_candidate.c.run_id == run_id,
            run_candidate.c.status == "ranked",
            run_candidate.c.rank <= top_n,
        )
        .order_by(run_candidate.c.rank)
    )
    return [
        EstimateTarget(
            row.candidate_id,
            row.rank,
            Address(
                street=row.address_line,
                unit=row.unit,
                city=row.city,
                state=row.state,
                zip5=row.zip5,
            ),
        )
        for row in rows
    ]


def save_estimate(connection: Connection, row: EstimateWrite) -> None:
    """Store an estimate; a second save for the same candidate and date replaces the first."""
    values = {
        "candidate_id": row.candidate_id,
        "fetched_on": row.fetched_on,
        "outcome": row.outcome,
        "address": row.address,
        "price": row.price,
        "price_low": row.price_low,
        "price_high": row.price_high,
        "comp_count": row.comp_count,
        "dropped_comp_count": row.dropped_comp_count,
        "comps": list(row.comps),
        "run_id": row.run_id,
    }
    statement = insert(candidate_estimate).values(values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[candidate_estimate.c.candidate_id, candidate_estimate.c.fetched_on],
            set_={name: statement.excluded[name] for name in values if name not in KEY_COLUMNS},
        )
    )


def latest_estimates(
    connection: Connection, candidate_ids: Collection[int], newest_on_or_before: date
) -> dict[int, StoredEstimate]:
    """Each candidate's newest estimate fetched on or before the date (none: no entry)."""
    if not candidate_ids:
        return {}
    newest = (
        select(candidate_estimate)
        .ext(distinct_on(candidate_estimate.c.candidate_id))
        .where(
            candidate_estimate.c.candidate_id.in_(list(candidate_ids)),
            candidate_estimate.c.fetched_on <= newest_on_or_before,
        )
        .order_by(candidate_estimate.c.candidate_id, candidate_estimate.c.fetched_on.desc())
    )
    return {row.candidate_id: StoredEstimate(**row._mapping) for row in connection.execute(newest)}


def billed_estimate_calls(connection: Connection, period: date) -> int:
    """Billed value-estimate calls in a billing period (cache hits and refunds are not
    billed). The cap counts every caller of the endpoint, `verify-rentcast` included."""
    return connection.execute(
        select(func.count())
        .select_from(api_request_log)
        .where(
            api_request_log.c.provider == "rentcast",
            api_request_log.c.endpoint == VALUE_ESTIMATE_ENDPOINT,
            api_request_log.c.period_start == period,
            api_request_log.c.billed,
        )
    ).scalar_one()

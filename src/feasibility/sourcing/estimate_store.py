"""All the SQL of value estimates: the stored answer per candidate and date, and the
count of billed estimate calls the monthly cap is measured against."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from feasibility.tables import api_request_log, candidate_estimate

# The endpoint literal the RentCast client logs for a value estimate.
VALUE_ESTIMATE_ENDPOINT = "/avm/value"
KEY_COLUMNS = ("candidate_id", "fetched_on")


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

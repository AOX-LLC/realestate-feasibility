"""All the sourcing SQL that is not a plain read. Matches first; the run's own tables are
added with the run itself."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, bindparam, func, update
from sqlalchemy.dialects.postgresql import insert

from feasibility.sourcing.matching import MatchResult, aggregate
from feasibility.tables import listing, listing_match


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

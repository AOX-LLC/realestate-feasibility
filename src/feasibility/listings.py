"""Stores listings from any source. first_seen_at is set once, on insert; the daily diff
keys on it."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from feasibility.sources.base import ListingBatch

UPSERT_LISTING = text(
    """
    INSERT INTO listing (source, external_id, market, address_line, unit, city, state, zip5, price,
        status, property_type, lot_size_sqft, living_area_sqft, year_built, listed_date,
        remarks, raw, first_seen_at, last_seen_at)
    VALUES (:source, :external_id, :market, :address_line, :unit, :city, :state, :zip5, :price,
        :status, :property_type, :lot_size_sqft, :living_area_sqft, :year_built, :listed_date,
        :remarks, CAST(:raw AS jsonb), COALESCE(:observed_at, now()),
        COALESCE(:observed_at, now()))
    ON CONFLICT (source, external_id) DO UPDATE
    SET market = EXCLUDED.market, address_line = EXCLUDED.address_line, unit = EXCLUDED.unit,
        city = EXCLUDED.city,
        state = EXCLUDED.state, zip5 = EXCLUDED.zip5, price = EXCLUDED.price,
        status = EXCLUDED.status, property_type = EXCLUDED.property_type,
        lot_size_sqft = EXCLUDED.lot_size_sqft, living_area_sqft = EXCLUDED.living_area_sqft,
        year_built = EXCLUDED.year_built, listed_date = EXCLUDED.listed_date,
        remarks = EXCLUDED.remarks, raw = EXCLUDED.raw,
        last_seen_at = CASE WHEN :fresh THEN COALESCE(:observed_at, now())
                            ELSE listing.last_seen_at END
    """
)


def upsert_listings(
    connection: Connection,
    market: str,
    batch: ListingBatch,
    observed_at: datetime | None = None,
) -> int:
    """Insert or refresh every listing in one batched statement.

    `observed_at` is the moment the feed was seen; it defaults to now() and is given only
    when replaying a recorded day (mock mode). first_seen_at is set once, on insert.
    last_seen_at moves on every fresh batch. A stale batch updates fields but not
    last_seen_at: nobody saw those listings today."""
    if not batch.listings:
        return 0
    rows: list[dict[str, Any]] = [
        {
            "source": listing.source,
            "external_id": listing.external_id,
            "market": market,
            "address_line": listing.address.street,
            "unit": listing.address.unit,
            "city": listing.address.city,
            "state": listing.address.state,
            "zip5": listing.address.zip5,
            "price": listing.price,
            "status": listing.status,
            "property_type": listing.property_type,
            "lot_size_sqft": listing.lot_size_sqft,
            "living_area_sqft": listing.living_area_sqft,
            "year_built": listing.year_built,
            "listed_date": listing.listed_date,
            "remarks": listing.remarks,
            "raw": json.dumps(listing.raw),
            "fresh": not batch.stale,
            "observed_at": observed_at,
        }
        for listing in batch.listings
    ]
    connection.execute(UPSERT_LISTING, rows)
    return len(rows)

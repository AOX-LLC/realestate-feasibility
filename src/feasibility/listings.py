"""Stores listings from any source. first_seen_at is set once, on insert; the daily diff
keys on it."""

import json
from typing import Any

from sqlalchemy import Connection, text

from feasibility.sources.base import ListingBatch

UPSERT_LISTING = text(
    """
    INSERT INTO listing (source, external_id, market, address_line, city, state, zip5, price,
        status, property_type, lot_size_sqft, living_area_sqft, year_built, listed_date,
        remarks, raw)
    VALUES (:source, :external_id, :market, :address_line, :city, :state, :zip5, :price,
        :status, :property_type, :lot_size_sqft, :living_area_sqft, :year_built, :listed_date,
        :remarks, CAST(:raw AS jsonb))
    ON CONFLICT (source, external_id) DO UPDATE
    SET market = EXCLUDED.market, address_line = EXCLUDED.address_line, city = EXCLUDED.city,
        state = EXCLUDED.state, zip5 = EXCLUDED.zip5, price = EXCLUDED.price,
        status = EXCLUDED.status, property_type = EXCLUDED.property_type,
        lot_size_sqft = EXCLUDED.lot_size_sqft, living_area_sqft = EXCLUDED.living_area_sqft,
        year_built = EXCLUDED.year_built, listed_date = EXCLUDED.listed_date,
        remarks = EXCLUDED.remarks, raw = EXCLUDED.raw,
        last_seen_at = CASE WHEN :fresh THEN now() ELSE listing.last_seen_at END
    """
)


def upsert_listings(connection: Connection, market: str, batch: ListingBatch) -> int:
    """Insert or refresh every listing in one batched statement. A stale batch updates
    fields but not last_seen_at: nobody saw those listings today."""
    if not batch.listings:
        return 0
    rows: list[dict[str, Any]] = [
        {
            "source": listing.source,
            "external_id": listing.external_id,
            "market": market,
            "address_line": listing.address.street,
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
        }
        for listing in batch.listings
    ]
    connection.execute(UPSERT_LISTING, rows)
    return len(rows)

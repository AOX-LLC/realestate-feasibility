"""Stores listings from any source. first_seen_at is set once, on insert; the daily diff
keys on it."""

import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Connection, Engine, text

from feasibility.markets.schema import ListingSourceSpec, MarketPack, RentCastListings
from feasibility.sources.base import ListingBatch, ListingQuery, ListingSource
from feasibility.sources.mls.stub import MlsListingSource
from feasibility.sources.rentcast.adapter import RentCastListingSource
from feasibility.sources.rentcast.client import RentCastClient

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
    WHERE listing.last_seen_at <= COALESCE(:observed_at, now())
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
    when replaying a recorded day (mock mode). A listing last seen later than `observed_at`
    is left alone, so replaying an earlier day never rolls it back. first_seen_at is set
    once, on insert.
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


def listing_query(pack: MarketPack, spec: ListingSourceSpec) -> ListingQuery:
    if isinstance(spec, RentCastListings):
        return ListingQuery(
            city=spec.city,
            state=spec.state,
            status=spec.status,
            days_old=spec.days_old,
            limit=spec.limit,
        )
    return ListingQuery(city=pack.market.county, state=pack.market.state, days_old=1, limit=500)


def sync_listings(
    engine: Engine,
    pack: MarketPack,
    client: RentCastClient,
    observed_at: datetime | None = None,
) -> Literal["fresh", "stale"]:
    """Fetch and store the listings of every enabled source in the pack.

    Returns "stale" when any source answered from an expired cache: those listings were
    stored but not seen today.
    """
    sync_status: Literal["fresh", "stale"] = "fresh"
    for spec in pack.sources.listings:
        if not spec.enabled:
            continue
        source: ListingSource = (
            RentCastListingSource(client)
            if isinstance(spec, RentCastListings)
            else MlsListingSource()
        )
        batch = source.fetch_listings(listing_query(pack, spec))
        with engine.begin() as connection:
            upsert_listings(connection, pack.market.id, batch, observed_at)
        if batch.stale:
            sync_status = "stale"
    return sync_status

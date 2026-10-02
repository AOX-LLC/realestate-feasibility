from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import select

from feasibility.api.deps import DEFAULT_PAGE_SIZE, EngineDep, Limit, MarketQuery
from feasibility.api.schemas import ListingOut, Page
from feasibility.tables import listing

router = APIRouter(prefix="/listings", tags=["listings"])

# The stored raw source record is internal; responses carry the normalized fields only.
LISTING_COLUMNS = [listing.c[name] for name in ListingOut.model_fields]


@router.get("")
def list_listings(
    engine: EngineDep,
    market: MarketQuery = "dallas",
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[ListingOut]:
    """Listings, oldest first, one keyset page at a time."""
    query = select(*LISTING_COLUMNS).where(listing.c.market == market)
    if after is not None:
        query = query.where(listing.c.id > after)
    query = query.order_by(listing.c.id).limit(limit + 1)

    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    items = [ListingOut.model_validate(dict(row)) for row in rows[:limit]]
    next_after = str(items[-1].id) if len(rows) > limit else None
    return Page[ListingOut](items=items, next_after=next_after)


@router.get("/{listing_id}")
def get_listing(engine: EngineDep, listing_id: Annotated[int, Path(ge=1)]) -> ListingOut:
    with engine.connect() as connection:
        row = (
            connection.execute(select(*LISTING_COLUMNS).where(listing.c.id == listing_id))
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such listing")
    return ListingOut.model_validate(dict(row))

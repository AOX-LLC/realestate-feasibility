from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import select

from feasibility.api.deps import DEFAULT_PAGE_SIZE, MARKET_ID, EngineDep, Limit, MarketQuery
from feasibility.api.schemas import Page, ParcelOut
from feasibility.tables import parcel

router = APIRouter(prefix="/parcels", tags=["parcels"])

ACCOUNT_ID = r"^[A-Za-z0-9]{1,32}$"
PARCEL_COLUMNS = [parcel.c[name] for name in ParcelOut.model_fields]


@router.get("")
def list_parcels(
    engine: EngineDep,
    market: MarketQuery = "dallas",
    zip5: Annotated[str | None, Query(pattern=r"^\d{5}$")] = None,
    after: Annotated[str | None, Query(pattern=ACCOUNT_ID)] = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> Page[ParcelOut]:
    """Parcels in account order, one keyset page at a time."""
    query = select(*PARCEL_COLUMNS).where(parcel.c.market == market)
    if zip5 is not None:
        query = query.where(parcel.c.zip5 == zip5)
    if after is not None:
        query = query.where(parcel.c.account_id > after)
    query = query.order_by(parcel.c.account_id).limit(limit + 1)

    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    items = [ParcelOut.model_validate(dict(row)) for row in rows[:limit]]
    next_after = items[-1].account_id if len(rows) > limit else None
    return Page[ParcelOut](items=items, next_after=next_after)


@router.get("/{market}/{account_id}")
def get_parcel(
    engine: EngineDep,
    market: Annotated[str, Path(pattern=MARKET_ID)],
    account_id: Annotated[str, Path(pattern=ACCOUNT_ID)],
) -> ParcelOut:
    query = select(*PARCEL_COLUMNS).where(
        parcel.c.market == market, parcel.c.account_id == account_id
    )
    with engine.connect() as connection:
        row = connection.execute(query).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such parcel")
    return ParcelOut.model_validate(dict(row))

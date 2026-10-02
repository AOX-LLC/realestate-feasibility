from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status

from feasibility.api.deps import MARKET_ID
from feasibility.api.schemas import BuyBoxOut, MarketDetail, MarketSummary
from feasibility.markets.loader import load_registry
from feasibility.markets.schema import MarketPack

router = APIRouter(prefix="/markets", tags=["markets"])


def _summary(pack: MarketPack) -> MarketSummary:
    return MarketSummary.model_validate(pack.market.model_dump())


@router.get("")
def list_markets() -> list[MarketSummary]:
    """Every configured market pack. There are only a handful, so no paging."""
    return [_summary(pack) for pack in load_registry().values()]


@router.get("/{market_id}")
def get_market(market_id: Annotated[str, Path(pattern=MARKET_ID)]) -> MarketDetail:
    pack = load_registry().get(market_id)
    if pack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such market")
    return MarketDetail(
        **_summary(pack).model_dump(),
        parcel_source=pack.sources.parcels.source,
        listing_sources=[spec.adapter for spec in pack.sources.listings if spec.enabled],
        buy_box=BuyBoxOut.model_validate(pack.buy_box.model_dump()),
        cost_assumptions_status=pack.cost_assumptions.status,
    )

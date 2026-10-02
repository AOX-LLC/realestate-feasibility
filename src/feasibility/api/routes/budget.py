from fastapi import APIRouter

from feasibility.api.deps import EngineDep, SettingsDep
from feasibility.api.schemas import BudgetOut
from feasibility.sources.rentcast.client import PROVIDER, RentCastClient

router = APIRouter(prefix="/budget", tags=["budget"])


@router.get("")
def get_budget(engine: EngineDep, settings: SettingsDep) -> BudgetOut:
    """RentCast requests spent in the current billing period. Mock mode spends nothing."""
    client = RentCastClient.from_settings(engine, settings)
    try:
        usage = client.budget_usage()
    finally:
        client.close()
    return BudgetOut(
        provider=PROVIDER,
        mode=settings.data_mode.value,
        period_start=usage.period_start,
        limit=usage.limit,
        used=usage.used,
        remaining=usage.remaining,
    )

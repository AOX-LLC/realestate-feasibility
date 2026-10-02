from datetime import UTC, datetime

from fastapi import APIRouter

from feasibility.api.deps import EngineDep, SettingsDep
from feasibility.api.schemas import BudgetOut
from feasibility.sources.rentcast import budget
from feasibility.sources.rentcast.client import PROVIDER

router = APIRouter(prefix="/budget", tags=["budget"])


@router.get("")
def get_budget(engine: EngineDep, settings: SettingsDep) -> BudgetOut:
    """RentCast requests spent in the current billing period. Mock mode spends nothing."""
    period = budget.period_start(datetime.now(UTC).date(), settings.rentcast_billing_anchor_day)
    with engine.connect() as connection:
        usage = budget.usage(connection, PROVIDER, period, settings.rentcast_monthly_budget)
    return BudgetOut(
        provider=PROVIDER,
        mode=settings.data_mode.value,
        period_start=usage.period_start,
        limit=usage.limit,
        used=usage.used,
        remaining=usage.remaining,
    )

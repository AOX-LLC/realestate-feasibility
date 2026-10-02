"""A hard monthly request budget per provider. A unit is reserved before every paid call
and refunded only when the provider cannot have billed it."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class BudgetUsage:
    period_start: date
    limit: int
    used: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


def period_start(today: date, anchor_day: int) -> date:
    """The start of the billing period containing today, for a period that renews on
    anchor_day (1-28) of each month."""
    if today.day >= anchor_day:
        return today.replace(day=anchor_day)
    if today.month == 1:
        return date(today.year - 1, 12, anchor_day)
    return date(today.year, today.month - 1, anchor_day)


def reserve(connection: Connection, provider: str, period: date, limit: int) -> bool:
    """Take one unit atomically. False when the period's limit is spent."""
    connection.execute(
        text(
            """
            INSERT INTO api_budget (provider, period_start, request_limit)
            VALUES (:provider, :period, :limit)
            ON CONFLICT (provider, period_start)
            DO UPDATE SET request_limit = EXCLUDED.request_limit
            """
        ),
        {"provider": provider, "period": period, "limit": limit},
    )
    row = connection.execute(
        text(
            """
            UPDATE api_budget SET used = used + 1
            WHERE provider = :provider AND period_start = :period AND used < request_limit
            RETURNING used
            """
        ),
        {"provider": provider, "period": period},
    ).first()
    return row is not None


def refund(connection: Connection, provider: str, period: date) -> None:
    connection.execute(
        text(
            """
            UPDATE api_budget SET used = used - 1
            WHERE provider = :provider AND period_start = :period AND used > 0
            """
        ),
        {"provider": provider, "period": period},
    )


def usage(connection: Connection, provider: str, period: date, limit: int) -> BudgetUsage:
    used = connection.execute(
        text("SELECT used FROM api_budget WHERE provider = :provider AND period_start = :period"),
        {"provider": provider, "period": period},
    ).scalar()
    return BudgetUsage(period_start=period, limit=limit, used=int(used or 0))

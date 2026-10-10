"""A hard monthly request budget per provider. A unit is reserved before every paid call
and refunded only when the provider cannot have billed it."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, Engine, text

SPEND_LOCK = {"key": "rentcast:estimate-spend"}


class SpendInProgressError(RuntimeError):
    """Another caller holds the spend lock: a paid call now could add to what it is counting."""


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


@contextmanager
def spend_lock(engine: Engine, *, wait: bool = True) -> Iterator[None]:
    """Hold the session-level lock that lets one spender at a time read the cap and the budget and
    act on them. No transaction stays open while it is held. By default waits for the lock; with
    `wait=False` raises `SpendInProgressError` at once if another caller holds it."""
    connection = engine.connect()
    held = False
    try:
        if wait:
            connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), SPEND_LOCK
            )
        else:
            taken = connection.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), SPEND_LOCK
            ).scalar_one()
            if not taken:
                connection.rollback()
                raise SpendInProgressError("a spend is in progress; try again")
        held = True
        connection.commit()
        try:
            yield
        finally:
            connection.rollback()
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), SPEND_LOCK
            )
            held = False
            connection.commit()
    finally:
        if held:
            # The unlock did not happen: end the session, which drops the lock, instead of
            # returning to the pool a connection that still holds it.
            connection.invalidate()
        connection.close()

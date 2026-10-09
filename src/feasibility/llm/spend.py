"""Spend caps, enforced by reservation.

Before each call a guard refuses when `spent + reservation > cap`. The reservation is the most a
single call may cost as agent-core estimates it (3 characters to a token, plus the full output
allowance); agent-core refuses any call whose estimate is over it. A call that is let through
is expected to cost no more, and an overshoot, if the estimate is ever wrong, is recorded at its
real cost. A call whose cost is unknown because it raised is counted at its reservation, so
recorded spend is not under-stated.

The guards do not lock. The metered client checks a guard and writes the row that counts against
it under one Postgres advisory lock, so callers that share a cap cannot both pass the same check.
A session guard with no database (an eval run in CI) is in-process only.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Engine

from feasibility.llm import ledger
from feasibility.llm.errors import LlmBudgetError

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SpendGuard(Protocol):
    def reserve(self, reservation: Decimal, *, billable: bool) -> None:
        """Raise LlmBudgetError when a call holding `reservation` could pass a cap. `billable`
        is the mode of the client making the call, which only that client knows."""

    def settle(self, spent: Decimal) -> None:
        """Count a finished call: its cost, or its reservation when the cost is unknown."""


class RunSpendGuard:
    """The caps of one sourcing run: the run's own spend, and (when its calls are billable) the
    month's. Both are read from the ledger, which the metered client writes before the next
    call, so there is nothing for `settle` to do."""

    def __init__(
        self,
        engine: Engine,
        run_id: int,
        run_cap: Decimal,
        monthly_cap: Decimal,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._engine = engine
        self._run_id = run_id
        self._run_cap = run_cap
        self._monthly_cap = monthly_cap
        self._clock = clock

    def reserve(self, reservation: Decimal, *, billable: bool) -> None:
        run_spent = ledger.run_spend(self._engine, self._run_id)
        if run_spent + reservation > self._run_cap:
            raise LlmBudgetError("run", spent=run_spent, reservation=reservation, cap=self._run_cap)
        if billable:
            month_spent = ledger.billable_spend_in_month(self._engine, self._clock())
            if month_spent + reservation > self._monthly_cap:
                raise LlmBudgetError(
                    "monthly", spent=month_spent, reservation=reservation, cap=self._monthly_cap
                )

    def settle(self, spent: Decimal) -> None:
        """Nothing to do: the run's spend is its ledger rows."""


class SessionSpendGuard:
    """The cap of an eval or recording session: an in-process total, plus the month's ledger cap
    when a database is configured and the session's calls are billable."""

    def __init__(
        self,
        max_usd: Decimal,
        *,
        engine: Engine | None = None,
        monthly_cap: Decimal | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if (engine is None) != (monthly_cap is None):
            raise ValueError("pass an engine and a monthly cap together, or neither")
        self._max_usd = max_usd
        self._engine = engine
        self._monthly_cap = monthly_cap
        self._clock = clock
        self._spent = Decimal(0)

    @property
    def spent(self) -> Decimal:
        return self._spent

    def reserve(self, reservation: Decimal, *, billable: bool) -> None:
        if self._spent + reservation > self._max_usd:
            raise LlmBudgetError(
                "session", spent=self._spent, reservation=reservation, cap=self._max_usd
            )
        if billable and self._engine is not None and self._monthly_cap is not None:
            month_spent = ledger.billable_spend_in_month(self._engine, self._clock())
            if month_spent + reservation > self._monthly_cap:
                raise LlmBudgetError(
                    "monthly", spent=month_spent, reservation=reservation, cap=self._monthly_cap
                )

    def settle(self, spent: Decimal) -> None:
        self._spent += spent

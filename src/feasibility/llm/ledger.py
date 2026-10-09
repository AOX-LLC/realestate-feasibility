"""The model call ledger: every call writes one row, whatever its outcome, on its own connection
so a rolled-back run keeps its spend. All SQL for the ledger lives here.

Spend is `sum(coalesce(cost_usd, reserved_usd))`: a call that raised has no known cost, so it
counts at the reservation held for it. The total is therefore over-stated, never under-stated.

A call's row is written before the call, as a `provider_error` at its reservation, and updated
when the call ends. A process killed mid-call therefore leaves that row behind: it reads as a
failed call that cost its reservation, which is the safe thing to assume.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from feasibility.tables import llm_call

ZERO = Decimal(0)
# Key of the advisory lock that serialises "check the caps, then write the pending row".
SPEND_LOCK_KEY = "llm_spend"
_SPEND = func.coalesce(llm_call.c.cost_usd, llm_call.c.reserved_usd)


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One call as the ledger stores it. `billable` is derived from the mode, not given."""

    stage: str
    prompt_id: str
    prompt_version: int
    input_sha256: str
    tier: str
    mode: str
    outcome: str
    reserved_usd: Decimal
    run_id: int | None = None
    candidate_id: int | None = None
    model: str | None = None
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    called_at: datetime | None = None


def insert_call(connection: Connection, call: LlmCallRecord) -> int:
    """Insert one ledger row on `connection` and return its id."""
    values: dict[str, Any] = {
        "run_id": call.run_id,
        "candidate_id": call.candidate_id,
        "stage": call.stage,
        "prompt_id": call.prompt_id,
        "prompt_version": call.prompt_version,
        "input_sha256": call.input_sha256,
        "tier": call.tier,
        "model": call.model,
        "mode": call.mode,
        "billable": call.mode in ("record", "live"),
        "outcome": call.outcome,
        "attempts": call.attempts,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cache_creation_input_tokens": call.cache_creation_input_tokens,
        "cache_read_input_tokens": call.cache_read_input_tokens,
        "cost_usd": call.cost_usd,
        "reserved_usd": call.reserved_usd,
        "latency_ms": call.latency_ms,
    }
    if call.called_at is not None:
        values["called_at"] = call.called_at
    row_id = connection.execute(
        insert(llm_call).values(**values).returning(llm_call.c.id)
    ).scalar_one()
    return int(row_id)


def record_call(engine: Engine, call: LlmCallRecord) -> int:
    """Insert one ledger row and return its id. Commits on its own connection."""
    with engine.begin() as connection:
        return insert_call(connection, call)


def lock_spend(connection: Connection) -> None:
    """Hold the spend lock until `connection`'s transaction ends. Whoever checks a cap and then
    writes the row that counts against it does both under this lock, so two processes cannot
    both pass the same check."""
    connection.execute(select(func.pg_advisory_xact_lock(func.hashtext(SPEND_LOCK_KEY))))


def finish_call(
    engine: Engine,
    call_id: int,
    *,
    outcome: str,
    reserved_usd: Decimal,
    attempts: int,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record how a call ended, on the row written before it. Commits on its own connection."""
    values = {
        "outcome": outcome,
        "reserved_usd": reserved_usd,
        "attempts": attempts,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }
    with engine.begin() as connection:
        connection.execute(update(llm_call).where(llm_call.c.id == call_id).values(**values))


def discard_call(engine: Engine, call_id: int) -> None:
    """Remove the row of a call that never reached the model (an error in our own code)."""
    with engine.begin() as connection:
        connection.execute(delete(llm_call).where(llm_call.c.id == call_id))


def run_spend(engine: Engine, run_id: int) -> Decimal:
    """Spent by one run across every attempt of it, in any mode."""
    query = select(func.coalesce(func.sum(_SPEND), ZERO)).where(llm_call.c.run_id == run_id)
    with engine.connect() as connection:
        return Decimal(connection.execute(query).scalar_one())


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing `now`, as a half-open interval."""
    moment = now.astimezone(UTC)
    start = datetime(moment.year, moment.month, 1, tzinfo=UTC)
    following = (
        datetime(moment.year + 1, 1, 1, tzinfo=UTC)
        if moment.month == 12
        else datetime(moment.year, moment.month + 1, 1, tzinfo=UTC)
    )
    return start, following


def billable_spend_in_month(engine: Engine, now: datetime) -> Decimal:
    """Spent by billable calls (record and live) in the UTC calendar month containing `now`."""
    start, following = month_bounds(now)
    query = select(func.coalesce(func.sum(_SPEND), ZERO)).where(
        llm_call.c.billable, llm_call.c.called_at >= start, llm_call.c.called_at < following
    )
    with engine.connect() as connection:
        return Decimal(connection.execute(query).scalar_one())

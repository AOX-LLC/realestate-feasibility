"""The delivery ledger: all its SQL, and the lock that keeps two deliveries of one run apart.

A row is written `sending` before an outbound call and finished after it (the same pending-row
pattern as the model-call ledger), each in its own short transaction: no call to a service is ever
made inside a transaction. A row still `sending` after ten minutes is read as `unknown`: the
process that wrote it is gone and nobody knows whether the service acted.

Nothing here takes a payload, a URL or a service's words. The table's own checks refuse them too.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, text

from feasibility.delivery.errors import DeliveryBusyError

STALE_SENDING_MINUTES = 10
FINISHED = ("sent", "failed", "unknown", "skipped")

# A `sending` row older than STALE_SENDING_MINUTES reads as `unknown`.
_READ_STATUS = (
    "CASE WHEN status = 'sending' "
    f"AND updated_at < now() - interval '{STALE_SENDING_MINUTES} minutes' "
    "THEN 'unknown' ELSE status END"
)


@dataclass(frozen=True, slots=True)
class ItemState:
    status: str
    attempts: int
    remote_ref: str | None
    content_sha256: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class Remembered:
    """The newest delivered row of an item in any run: where it went and what it said."""

    run_id: int
    remote_ref: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    target: str
    item: str
    mode: str
    status: str
    attempts: int
    error_code: str | None
    remote_ref: str | None
    updated_at: datetime


@contextmanager
def run_lock(engine: Engine, run_id: int) -> Generator[None]:
    """Hold the delivery lock of a run, or raise `DeliveryBusyError` at once.

    A session-level advisory lock on a connection of its own, outside any transaction: it lives
    as long as this process does, so a crash frees it, and a second delivery of the same run (the
    worker and a person at the command line, say) cannot post a second digest while the first is
    in the middle of its call."""
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    key = text("SELECT hashtextextended('delivery:' || CAST(:run AS text), 0)")
    try:
        lock = connection.execute(key, {"run": run_id}).scalar_one()
        taken = connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock})
        if not taken.scalar_one():
            raise DeliveryBusyError("busy")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock})
    finally:
        connection.close()


def read(engine: Engine, run_id: int, target: str, item: str, mode: str) -> ItemState | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT {_READ_STATUS} AS status, attempts, remote_ref, content_sha256, "  # noqa: S608
                "error_code FROM delivery "
                "WHERE run_id = :run AND target = :target AND item = :item AND mode = :mode"
            ),
            {"run": run_id, "target": target, "item": item, "mode": mode},
        ).one_or_none()
    return None if row is None else ItemState(*row)


def start(
    engine: Engine, run_id: int, target: str, item: str, mode: str, content_sha256: str
) -> None:
    """Record that a call is about to be made: `sending`, one more attempt, committed first."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO delivery (run_id, target, item, mode, status, content_sha256, "
                "attempts) VALUES (:run, :target, :item, :mode, 'sending', :sha, 1) "
                "ON CONFLICT (run_id, target, item, mode) DO UPDATE SET status = 'sending', "
                "content_sha256 = excluded.content_sha256, error_code = NULL, "
                "attempts = least(delivery.attempts + 1, 32000), updated_at = now()"
            ),
            {"run": run_id, "target": target, "item": item, "mode": mode, "sha": content_sha256},
        )


def finish(
    engine: Engine,
    run_id: int,
    target: str,
    item: str,
    mode: str,
    status: str,
    *,
    remote_ref: str | None = None,
    error_code: str | None = None,
) -> None:
    """End a call: `sent` with where it went, or `failed` / `unknown` with a short code."""
    if status not in ("sent", "failed", "unknown"):
        raise ValueError(f"a call ends sent, failed or unknown, not {status!r}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE delivery SET status = :status, remote_ref = coalesce(:ref, remote_ref), "
                "error_code = :code, updated_at = now() "
                "WHERE run_id = :run AND target = :target AND item = :item AND mode = :mode"
            ),
            {
                "status": status,
                "ref": remote_ref,
                "code": None if status == "sent" else error_code,
                "run": run_id,
                "target": target,
                "item": item,
                "mode": mode,
            },
        )


def note_skipped(
    engine: Engine,
    run_id: int,
    target: str,
    item: str,
    mode: str,
    content_sha256: str,
    remote_ref: str,
) -> None:
    """Record that this run's item needed no call because the same content is already there. A
    row this run already has is left as it is."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO delivery (run_id, target, item, mode, status, content_sha256, "
                "remote_ref, attempts) VALUES (:run, :target, :item, :mode, 'skipped', :sha, "
                ":ref, 0) ON CONFLICT (run_id, target, item, mode) DO NOTHING"
            ),
            {
                "run": run_id,
                "target": target,
                "item": item,
                "mode": mode,
                "sha": content_sha256,
                "ref": remote_ref,
            },
        )


def remembered(engine: Engine, target: str, item: str, mode: str) -> Remembered | None:
    """The newest `sent` or `skipped` row of this item in any run, if it has a remote reference."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT run_id, remote_ref, content_sha256 FROM delivery "
                "WHERE target = :target AND item = :item AND mode = :mode "
                "AND status IN ('sent', 'skipped') AND remote_ref IS NOT NULL "
                "ORDER BY updated_at DESC, id DESC LIMIT 1"
            ),
            {"target": target, "item": item, "mode": mode},
        ).one_or_none()
    return None if row is None else Remembered(*row)


def drop(engine: Engine, run_id: int, target: str, mode: str) -> int:
    """Forget the rows of a run's target and mode whose outcome is not settled (`unknown`,
    `sending` or `failed`), for an operator's `--resend`. What was `sent` stays: a resend posts
    again only what may be missing, never the digest that is already there."""
    with engine.begin() as connection:
        return connection.execute(
            text(
                "DELETE FROM delivery WHERE run_id = :run AND target = :target AND mode = :mode "
                "AND status IN ('unknown', 'sending', 'failed')"
            ),
            {"run": run_id, "target": target, "mode": mode},
        ).rowcount


def rows_for_run(engine: Engine, run_id: int) -> list[DeliveryRow]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                f"SELECT target, item, mode, {_READ_STATUS} AS status, attempts, error_code, "  # noqa: S608
                "remote_ref, updated_at FROM delivery WHERE run_id = :run "
                "ORDER BY target DESC, mode, id"
            ),
            {"run": run_id},
        ).all()
    return [DeliveryRow(*row) for row in found]

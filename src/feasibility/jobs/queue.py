"""A Postgres job queue: enqueue, claim with SKIP LOCKED, complete, fail with backoff,
and requeue jobs whose worker lease expired."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, text

ERROR_TEXT_LIMIT = 2000
DEFAULT_PRIORITY = 100
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    connection: Connection,
    kind: str,
    payload: BaseModel,
    *,
    priority: int = DEFAULT_PRIORITY,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int | None:
    """Queue a job. Returns its id, or None when a job with the same dedupe key is
    already queued or running."""
    row = connection.execute(
        text(
            """
            INSERT INTO job (kind, payload, priority, run_after, dedupe_key, max_attempts)
            VALUES (:kind, CAST(:payload AS jsonb), :priority, COALESCE(:run_after, now()),
                    :dedupe_key, :max_attempts)
            ON CONFLICT (dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING
            RETURNING id
            """
        ),
        {
            "kind": kind,
            "payload": payload.model_dump_json(),
            "priority": priority,
            "run_after": run_after,
            "dedupe_key": dedupe_key,
            "max_attempts": max_attempts,
        },
    ).first()
    return None if row is None else int(row.id)


def latest_active_morning_date(connection: Connection, market: str) -> date | None:
    """The latest date a queued or running `morning.run` job of the market is for."""
    found: date | None = connection.execute(
        text(
            """
            SELECT max(CAST(payload->>'as_of' AS date))
            FROM job
            WHERE kind = 'morning.run'
              AND status IN ('queued', 'running')
              AND payload->>'market' = :market
              AND payload->>'as_of' IS NOT NULL
            """
        ),
        {"market": market},
    ).scalar_one()
    return found


def claim(connection: Connection, worker_id: str, lease: timedelta) -> ClaimedJob | None:
    """Take the next runnable job, counting the attempt. Concurrent workers never get
    the same job: rows locked by another claim are skipped."""
    row = connection.execute(
        text(
            """
            UPDATE job
            SET status = 'running', locked_by = :worker_id, locked_until = now() + :lease,
                attempts = attempts + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM job
                WHERE status = 'queued' AND run_after <= now()
                ORDER BY priority, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, kind, payload, attempts, max_attempts
            """
        ),
        {"worker_id": worker_id, "lease": lease},
    ).first()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


def complete(connection: Connection, job_id: int, worker_id: str) -> bool:
    """Mark a job done. False when this worker no longer holds it (its lease expired
    and the job was requeued)."""
    result = connection.execute(
        text(
            """
            UPDATE job
            SET status = 'done', locked_by = NULL, locked_until = NULL, last_error = NULL,
                finished_at = now(), updated_at = now()
            WHERE id = :job_id AND status = 'running' AND locked_by = :worker_id
            """
        ),
        {"job_id": job_id, "worker_id": worker_id},
    )
    return result.rowcount == 1


def fail(
    connection: Connection, job_id: int, worker_id: str, error: str, *, permanent: bool = False
) -> str | None:
    """Record a failed attempt. The job is retried after 2^attempts minutes, or goes to
    'dead' once it has used max_attempts or when the failure is permanent (retrying
    cannot help). Returns the new status, or None when this worker no longer holds it."""
    row = connection.execute(
        text(
            """
            UPDATE job
            SET status = CASE WHEN (:permanent OR attempts >= max_attempts) THEN 'dead'
                              ELSE 'queued' END,
                run_after = CASE WHEN (:permanent OR attempts >= max_attempts) THEN run_after
                                 ELSE now() + make_interval(mins => power(2, attempts)::int)
                            END,
                finished_at = CASE WHEN (:permanent OR attempts >= max_attempts) THEN now() END,
                locked_by = NULL, locked_until = NULL, last_error = :error, updated_at = now()
            WHERE id = :job_id AND status = 'running' AND locked_by = :worker_id
            RETURNING status
            """
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "error": error[:ERROR_TEXT_LIMIT],
            "permanent": permanent,
        },
    ).first()
    return None if row is None else str(row.status)


def reclaim_expired(connection: Connection) -> int:
    """Requeue running jobs whose lease ran out (the worker died or hung). A job that
    has already used every attempt goes to 'dead' instead of looping forever."""
    result = connection.execute(
        text(
            """
            UPDATE job
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                last_error = 'lease expired', locked_by = NULL, locked_until = NULL,
                updated_at = now()
            WHERE status = 'running' AND locked_until < now()
            """
        )
    )
    return result.rowcount

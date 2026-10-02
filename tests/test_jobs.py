from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import Engine, text

from feasibility.config import DataMode, Settings
from feasibility.jobs import queue
from feasibility.jobs.handlers import JobContext, JobKind, UnknownJobKindError, enqueue_job
from feasibility.jobs.worker import Worker

LEASE = timedelta(minutes=5)


class EchoPayload(BaseModel):
    message: str


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


def _job(engine: Engine, job_id: int) -> Any:
    with engine.connect() as connection:
        return connection.execute(text("SELECT * FROM job WHERE id = :id"), {"id": job_id}).one()


def _enqueue(engine: Engine, message: str = "hi", **options: Any) -> int:
    with engine.begin() as connection:
        job_id = queue.enqueue(connection, "echo", EchoPayload(message=message), **options)
    assert job_id is not None
    return job_id


def test_claim_complete_round_trip(engine: Engine) -> None:
    job_id = _enqueue(engine)

    with engine.begin() as connection:
        claimed = queue.claim(connection, "w1", LEASE)
        assert claimed is not None
        assert (claimed.id, claimed.kind, claimed.payload) == (job_id, "echo", {"message": "hi"})
        assert queue.claim(connection, "w2", LEASE) is None
        assert queue.complete(connection, job_id, "w1")

    row = _job(engine, job_id)
    assert (row.status, row.attempts, row.locked_by) == ("done", 1, None)


def test_claim_order_is_priority_then_age(engine: Engine) -> None:
    low = _enqueue(engine, "low", priority=200)
    first = _enqueue(engine, "first")
    second = _enqueue(engine, "second")

    with engine.begin() as connection:
        order = [queue.claim(connection, "w", LEASE) for _ in range(3)]

    assert [job.id for job in order if job] == [first, second, low]


def test_future_jobs_are_not_claimed(engine: Engine) -> None:
    with engine.begin() as connection:
        later = connection.execute(text("SELECT now() + interval '1 hour'")).scalar_one()
    _enqueue(engine, run_after=later)

    with engine.begin() as connection:
        assert queue.claim(connection, "w", LEASE) is None


def test_concurrent_claims_never_share_a_job(engine: Engine) -> None:
    first_id = _enqueue(engine, "a")
    second_id = _enqueue(engine, "b")

    with engine.connect() as first, engine.connect() as second:
        first.begin()
        second.begin()
        claimed_first = queue.claim(first, "w1", LEASE)
        claimed_second = queue.claim(second, "w2", LEASE)
        first.commit()
        second.commit()

    assert claimed_first is not None and claimed_second is not None
    assert {claimed_first.id, claimed_second.id} == {first_id, second_id}


def test_dedupe_key_blocks_a_second_active_job_only(engine: Engine) -> None:
    first = _enqueue(engine, dedupe_key="sync:dallas")
    with engine.begin() as connection:
        duplicate = queue.enqueue(
            connection, "echo", EchoPayload(message="x"), dedupe_key="sync:dallas"
        )
    assert duplicate is None

    with engine.begin() as connection:
        queue.claim(connection, "w", LEASE)
        queue.complete(connection, first, "w")
    assert _enqueue(engine, dedupe_key="sync:dallas") != first


def test_failure_backs_off_then_dies_at_max_attempts(engine: Engine) -> None:
    job_id = _enqueue(engine, max_attempts=2)

    with engine.begin() as connection:
        queue.claim(connection, "w", LEASE)
        assert queue.fail(connection, job_id, "w", "first") == "queued"
        delay = connection.execute(
            text("SELECT run_after - now() FROM job WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert timedelta(minutes=1, seconds=50) < delay <= timedelta(minutes=2)

    with engine.begin() as connection:
        connection.execute(text("UPDATE job SET run_after = now() WHERE id = :id"), {"id": job_id})
        queue.claim(connection, "w", LEASE)
        assert queue.fail(connection, job_id, "w", "second") == "dead"

    row = _job(engine, job_id)
    assert (row.status, row.attempts, row.last_error) == ("dead", 2, "second")


def test_expired_lease_is_requeued_and_old_holder_cannot_finish(engine: Engine) -> None:
    job_id = _enqueue(engine)
    with engine.begin() as connection:
        queue.claim(connection, "w1", LEASE)
        connection.execute(
            text("UPDATE job SET locked_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        assert queue.reclaim_expired(connection) == 1
        assert not queue.complete(connection, job_id, "w1")
        assert queue.fail(connection, job_id, "w1", "late") is None

    assert _job(engine, job_id).status == "queued"


def _registry(handler: Any) -> dict[str, JobKind]:
    return {"echo": JobKind(payload_model=EchoPayload, handler=handler)}


def test_enqueue_job_validates_the_payload(engine: Engine) -> None:
    registry = _registry(lambda payload, context: None)
    with engine.begin() as connection:
        with pytest.raises(ValidationError):
            enqueue_job(connection, registry, "echo", {"wrong": 1})
        with pytest.raises(UnknownJobKindError):
            enqueue_job(connection, registry, "nope", {})
        assert enqueue_job(connection, registry, "echo", {"message": "ok"}) is not None


def test_worker_runs_a_job_to_done(engine: Engine) -> None:
    seen: list[tuple[str, int]] = []

    def handler(payload: EchoPayload, context: JobContext) -> None:
        seen.append((payload.message, context.job_id))

    job_id = _enqueue(engine, "hello")
    worker = Worker(engine, _settings(), _registry(handler), worker_id="w")

    assert worker.run_once()
    assert not worker.run_once()
    assert seen == [("hello", job_id)]
    assert _job(engine, job_id).status == "done"


def test_worker_records_a_redacted_failure(engine: Engine) -> None:
    sentinel = "sentinel-secret-77"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SecretStr(sentinel)
    )

    def handler(payload: EchoPayload, context: JobContext) -> None:
        raise RuntimeError(f"upstream said {sentinel}")

    job_id = _enqueue(engine)
    worker = Worker(engine, settings, _registry(handler), worker_id="w")

    assert worker.run_once()
    row = _job(engine, job_id)
    assert row.status == "queued"
    assert sentinel not in row.last_error
    assert row.last_error.startswith("RuntimeError: upstream said")

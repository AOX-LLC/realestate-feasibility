"""The worker loop: requeue expired leases, claim one job, run it, record the outcome."""

import logging
import os
import socket
import threading
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import Engine

from feasibility.config import Settings
from feasibility.jobs import queue
from feasibility.jobs.handlers import JobContext, Registry, lookup
from feasibility.logging import redact

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_LEASE = timedelta(minutes=30)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_job(job: queue.ClaimedJob, registry: Registry, context: JobContext) -> None:
    """Validate the payload and run the handler for one claimed job.

    This is the single seam around job execution; tracing wraps it.
    """
    kind = lookup(registry, job.kind)
    payload: BaseModel = kind.payload_model.model_validate(job.payload)
    kind.handler(payload, context)


def describe_failure(error: Exception, secrets: list[str]) -> str:
    return redact(f"{type(error).__name__}: {error}", secrets)


class Worker:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        registry: Registry,
        *,
        worker_id: str | None = None,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._registry = registry
        self._worker_id = worker_id or default_worker_id()
        self._lease = lease

    def run_once(self) -> bool:
        """Run at most one job. Returns whether a job was claimed."""
        with self._engine.begin() as connection:
            reclaimed = queue.reclaim_expired(connection)
            job = queue.claim(connection, self._worker_id, self._lease)
        if reclaimed:
            log.warning("requeued %d job(s) with expired leases", reclaimed)
        if job is None:
            return False

        log.info("job %d (%s) attempt %d started", job.id, job.kind, job.attempts)
        context = JobContext(engine=self._engine, settings=self._settings, job_id=job.id)
        try:
            run_job(job, self._registry, context)
        except Exception as error:  # a failing handler must not stop the worker
            self._record_failure(job, error)
            return True

        with self._engine.begin() as connection:
            if not queue.complete(connection, job.id, self._worker_id):
                log.warning("job %d finished after its lease was taken back", job.id)
        log.info("job %d (%s) done", job.id, job.kind)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        log.info("worker %s polling every %.0fs", self._worker_id, POLL_INTERVAL_SECONDS)
        while not stop.is_set():
            if not self.run_once():
                stop.wait(POLL_INTERVAL_SECONDS)

    def _record_failure(self, job: queue.ClaimedJob, error: Exception) -> None:
        message = describe_failure(error, self._settings.secret_values())
        with self._engine.begin() as connection:
            status = queue.fail(connection, job.id, self._worker_id, message)
        log.error("job %d (%s) failed, now %s: %s", job.id, job.kind, status, message)

"""The job registry: each job kind maps to a payload model and a handler.

Payloads are validated against the model when a job is enqueued and again when it runs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from feasibility.config import Settings
from feasibility.jobs import queue


@dataclass(frozen=True)
class JobContext:
    engine: Engine
    settings: Settings
    job_id: int


@dataclass(frozen=True)
class JobKind:
    payload_model: type[BaseModel]
    handler: Callable[[Any, JobContext], None]


class UnknownJobKindError(ValueError):
    def __init__(self, kind: str, known: Mapping[str, JobKind]) -> None:
        super().__init__(f"unknown job kind {kind!r}; known kinds: {', '.join(sorted(known))}")


Registry = Mapping[str, JobKind]


def lookup(registry: Registry, kind: str) -> JobKind:
    if kind not in registry:
        raise UnknownJobKindError(kind, registry)
    return registry[kind]


def enqueue_job(
    connection: Connection,
    registry: Registry,
    kind: str,
    payload: Mapping[str, Any],
    *,
    dedupe_key: str | None = None,
) -> int | None:
    """Validate a raw payload against its kind's model, then queue the job."""
    model = lookup(registry, kind).payload_model.model_validate(payload)
    return queue.enqueue(connection, kind, model, dedupe_key=dedupe_key)


def build_registry() -> dict[str, JobKind]:
    """Every job kind this application runs."""
    return {}

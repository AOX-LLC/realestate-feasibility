"""The job registry: each job kind maps to a payload model and a handler.

Payloads are validated against the model when a job is enqueued and again when it runs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine

from feasibility.config import Settings
from feasibility.jobs import queue
from feasibility.jobs.payloads import (
    CadImportPayload,
    ListingsSyncPayload,
    SourcingRunPayload,
)
from feasibility.listings import sync_listings
from feasibility.llm.run import PermanentModelError
from feasibility.markets.loader import PackError, get_pack
from feasibility.sources.base import ImportRequest, NotConfiguredError
from feasibility.sources.cad_csv.importer import CadCsvParcelSource, CadImportError
from feasibility.sources.mls.reso import remarks_source_for
from feasibility.sources.rentcast.client import (
    BudgetExhaustedError,
    RentCastClient,
    SchemaDriftError,
)
from feasibility.sourcing.errors import SourcingError
from feasibility.sourcing.run import run_sourcing


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


# Failures a retry cannot fix. Retrying some of them would also spend paid requests
# (schema drift is billed), so they go straight to 'dead'.
PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    UnknownJobKindError,
    ValidationError,
    PackError,
    CadImportError,
    NotConfiguredError,
    SchemaDriftError,
    BudgetExhaustedError,
    SourcingError,
    # A recording that is missing or a model that is misconfigured is the same on every attempt.
    PermanentModelError,
)


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


def resolve_local_file(local_dir: Path, name: str) -> Path:
    root = local_dir.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{name!r} is outside the local data directory")
    return path


def run_cad_import(payload: CadImportPayload, context: JobContext) -> None:
    source = CadCsvParcelSource(context.engine, get_pack(payload.market))
    source.import_archive(
        ImportRequest(
            archive=resolve_local_file(context.settings.local_dir, payload.archive),
            kind=payload.kind,
            roll_year=payload.roll_year,
            file_date=payload.file_date,
            force=payload.force,
        )
    )


def run_listings_sync(payload: ListingsSyncPayload, context: JobContext) -> None:
    """Fetch new listings from every enabled source in the market pack and store them."""
    pack = get_pack(payload.market)
    client = RentCastClient.from_settings(context.engine, context.settings)
    try:
        sync_listings(
            context.engine, pack, client, remarks_source_for(context.settings, pack.market.id)
        )
    finally:
        client.close()


def run_sourcing_job(payload: SourcingRunPayload, context: JobContext) -> None:
    run_sourcing(context.engine, context.settings, payload.market, payload.as_of)


def build_registry() -> dict[str, JobKind]:
    """Every job kind this application runs."""
    return {
        "cad.import": JobKind(CadImportPayload, run_cad_import),
        "listings.sync": JobKind(ListingsSyncPayload, run_listings_sync),
        "sourcing.run": JobKind(SourcingRunPayload, run_sourcing_job),
    }

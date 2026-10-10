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
from feasibility.delivery.build import BriefError, build_and_store
from feasibility.delivery.errors import PdfRenderError
from feasibility.jobs import queue
from feasibility.jobs.payloads import (
    BriefDeliverPayload,
    CadImportPayload,
    ListingsSyncPayload,
    MorningRunPayload,
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
from feasibility.sourcing import store as sourcing_store
from feasibility.sourcing.dates import resolve_run_date
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
    # A brief that cannot be built (no such run, a run still running) is the same on every try.
    BriefError,
    # The renderer refused a fetch: the same document is refused the same way.
    PdfRenderError,
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


def _queue_brief(context: JobContext, run_id: int) -> None:
    with context.engine.begin() as connection:
        queue.enqueue(
            connection,
            "brief.deliver",
            BriefDeliverPayload(run_id=run_id),
            dedupe_key=f"brief.deliver:{run_id}",
        )


def run_morning(payload: MorningRunPayload, context: JobContext) -> None:
    """Source the day, then queue its brief.

    A run that ranked but whose model stages could not finish (a missing recording, a model
    that is misconfigured) still has a ranking and pro-formas: its brief is queued, partial,
    and the job still ends dead so the failure stays visible. A model provider that is down
    (retryable) queues nothing: the retry reuses every cached result and briefs when it
    finishes. Any other failure queues nothing."""
    try:
        result = run_sourcing(context.engine, context.settings, payload.market, payload.as_of)
    except PermanentModelError:
        # The date the trigger resolved; asking again could answer differently (a live run that
        # crossed midnight) and lose the brief.
        run_date = (
            payload.as_of or resolve_run_date(context.settings, get_pack(payload.market), None)[0]
        )
        with context.engine.connect() as connection:
            run_id = sourcing_store.run_id_of(connection, payload.market, run_date)
        if run_id is not None:
            _queue_brief(context, run_id)
        raise
    _queue_brief(context, result.run_id)


def run_brief_deliver(payload: BriefDeliverPayload, context: JobContext) -> None:
    """Build the run's brief and store it. (Delivery to the outside comes in a later session.)"""
    build_and_store(context.engine, payload.run_id)


def build_registry() -> dict[str, JobKind]:
    """Every job kind this application runs."""
    return {
        "cad.import": JobKind(CadImportPayload, run_cad_import),
        "listings.sync": JobKind(ListingsSyncPayload, run_listings_sync),
        "sourcing.run": JobKind(SourcingRunPayload, run_sourcing_job),
        "morning.run": JobKind(MorningRunPayload, run_morning),
        "brief.deliver": JobKind(BriefDeliverPayload, run_brief_deliver),
    }

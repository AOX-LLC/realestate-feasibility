"""The job registry: each job kind maps to a payload model and a handler.

Payloads are validated against the model when a job is enqueued and again when it runs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine

from feasibility.config import Settings
from feasibility.delivery.build import BriefError
from feasibility.delivery.deliver import deliver_brief
from feasibility.delivery.errors import (
    DeliveryConfigError,
    DeliveryUnknownOutcomeError,
    PdfRenderError,
)
from feasibility.jobs import queue
from feasibility.jobs.payloads import (
    BriefDeliverPayload,
    CadImportPayload,
    ListingsSyncPayload,
    MorningRunPayload,
    RetentionPrunePayload,
    SourcingRunPayload,
)
from feasibility.listings import sync_listings
from feasibility.llm.run import PermanentModelError
from feasibility.markets.loader import PackError, get_pack
from feasibility.retention.prune import (
    RetentionRefusedError,
    check_as_of_allowed,
    policy_from,
    prune,
)
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
    # A prune from a chosen day where that is not allowed: queueing it again changes nothing.
    RetentionRefusedError,
    # The renderer refused a fetch: the same document is refused the same way.
    PdfRenderError,
    # Credentials, a channel or a database that is wrong stay wrong; retrying would only repeat the
    # request. (An incomplete delivery is not here: a retry sends only what is left.)
    DeliveryConfigError,
    # A Slack item may have been sent. Only a person can look in the channel and decide.
    DeliveryUnknownOutcomeError,
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
    """Build the run's brief and deliver it to the enabled targets (mock or live). It sends only
    what the ledger says is not there yet, so a retry or a second job for the run is harmless."""
    deliver_brief(context.engine, context.settings, payload.run_id)


def run_retention_prune(payload: RetentionPrunePayload, context: JobContext) -> None:
    """Delete what is past its retention window (or count it, for a dry run). Safe to repeat: a
    second run finds nothing left to delete."""
    check_as_of_allowed(context.engine, context.settings, payload.as_of, payload.dry_run)
    prune(
        context.engine,
        policy_from(context.settings),
        as_of=payload.as_of or datetime.now(UTC).date(),
        dry_run=payload.dry_run,
    )


def build_registry() -> dict[str, JobKind]:
    """Every job kind this application runs."""
    return {
        "cad.import": JobKind(CadImportPayload, run_cad_import),
        "listings.sync": JobKind(ListingsSyncPayload, run_listings_sync),
        "sourcing.run": JobKind(SourcingRunPayload, run_sourcing_job),
        "morning.run": JobKind(MorningRunPayload, run_morning),
        "brief.deliver": JobKind(BriefDeliverPayload, run_brief_deliver),
        "retention.prune": JobKind(RetentionPrunePayload, run_retention_prune),
    }

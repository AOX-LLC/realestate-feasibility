"""The job registry: each job kind maps to a payload model and a handler.

Payloads are validated against the model when a job is enqueued and again when it runs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine

from feasibility.config import Settings
from feasibility.jobs import queue
from feasibility.listings import upsert_listings
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import FileKind, ListingSourceSpec, MarketPack, RentCastListings
from feasibility.sources.base import ImportRequest, ListingQuery, ListingSource
from feasibility.sources.cad_csv.importer import CadCsvParcelSource
from feasibility.sources.mls.stub import MlsListingSource
from feasibility.sources.rentcast.adapter import RentCastListingSource
from feasibility.sources.rentcast.client import RentCastClient


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


class CadImportPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str
    # A file name inside the local data directory; jobs never read files elsewhere.
    archive: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    kind: FileKind
    roll_year: int | None = None
    file_date: date | None = None
    force: bool = False


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


class ListingsSyncPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str


def listing_query(pack: MarketPack, spec: ListingSourceSpec) -> ListingQuery:
    if isinstance(spec, RentCastListings):
        return ListingQuery(
            city=spec.city,
            state=spec.state,
            status=spec.status,
            days_old=spec.days_old,
            limit=spec.limit,
        )
    return ListingQuery(city=pack.market.county, state=pack.market.state, days_old=1, limit=500)


def run_listings_sync(payload: ListingsSyncPayload, context: JobContext) -> None:
    """Fetch new listings from every enabled source in the market pack and store them."""
    pack = get_pack(payload.market)
    client = RentCastClient.from_settings(context.engine, context.settings)
    try:
        for spec in pack.sources.listings:
            if not spec.enabled:
                continue
            source: ListingSource = (
                RentCastListingSource(client)
                if isinstance(spec, RentCastListings)
                else MlsListingSource()
            )
            batch = source.fetch_listings(listing_query(pack, spec))
            with context.engine.begin() as connection:
                upsert_listings(connection, pack.market.id, batch)
    finally:
        client.close()


def build_registry() -> dict[str, JobKind]:
    """Every job kind this application runs."""
    return {
        "cad.import": JobKind(CadImportPayload, run_cad_import),
        "listings.sync": JobKind(ListingsSyncPayload, run_listings_sync),
    }

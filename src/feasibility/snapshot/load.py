"""Loads the committed synthetic snapshot into the database: `feasibility seed`.

Idempotent and offline. The CAD files are zipped here with fixed member timestamps, so the
archive (and its sha256) is the same on every run and a second seed is a no-op; listings
come from the recorded RentCast responses through a snapshot transport, never the network,
and spend no budget.
"""

import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine

from feasibility.config import Settings
from feasibility.jobs.handlers import listing_query
from feasibility.listings import upsert_listings
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import MarketPack, RentCastListings
from feasibility.sources.base import ImportReport, ImportRequest
from feasibility.sources.cad_csv.importer import CadCsvParcelSource
from feasibility.sources.rentcast.adapter import RentCastListingSource
from feasibility.sources.rentcast.client import RentCastClient, Ttls
from feasibility.sources.rentcast.transport import SnapshotTransport

SET_KINDS: tuple[Literal["certified", "current"], ...] = ("certified", "current")


@dataclass
class SeedReport:
    certified: ImportReport | None
    current: ImportReport | None
    listings: int


def seed(engine: Engine, settings: Settings) -> SeedReport:
    pack = get_pack(settings.market)
    cad_dir = settings.snapshot_dir / "cad" / settings.market
    manifest = json.loads((cad_dir / "manifest.json").read_text(encoding="utf-8"))
    reports: dict[str, ImportReport | None] = {}
    with tempfile.TemporaryDirectory() as scratch:
        for kind in SET_KINDS:
            source_dir = cad_dir / kind
            reports[kind] = (
                _import_set(engine, pack, manifest, kind, source_dir, Path(scratch))
                if source_dir.is_dir()
                else None
            )
    return SeedReport(
        reports["certified"], reports["current"], _load_listings(engine, settings, pack)
    )


def _import_set(
    engine: Engine,
    pack: MarketPack,
    manifest: dict[str, Any],
    kind: Literal["certified", "current"],
    source_dir: Path,
    scratch: Path,
) -> ImportReport:
    file_date = date.fromisoformat(manifest["sets"][kind]["file_date"])
    archive = scratch / f"{pack.market.id}_{kind}.zip"
    _write_archive(archive, source_dir, pack, file_date)
    request = ImportRequest(
        archive=archive,
        kind=kind,
        roll_year=int(manifest["roll_year"]),
        file_date=file_date,
    )
    return CadCsvParcelSource(engine, pack).import_archive(request)


def _write_archive(archive: Path, source_dir: Path, pack: MarketPack, file_date: date) -> None:
    """Zip the committed CSVs byte for byte, with a fixed timestamp so the hash is stable."""
    stamp = datetime(file_date.year, file_date.month, file_date.day).timetuple()[:6]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for spec in pack.sources.parcels.files.values():
            info = zipfile.ZipInfo(spec.member, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, (source_dir / spec.member).read_bytes())


def _load_listings(engine: Engine, settings: Settings, pack: MarketPack) -> int:
    client = RentCastClient(
        engine,
        SnapshotTransport(settings.snapshot_dir / "rentcast"),
        live=False,
        monthly_budget=settings.rentcast_monthly_budget,
        billing_anchor_day=settings.rentcast_billing_anchor_day,
        ttls=Ttls(
            sale_listings=settings.rentcast_ttl_sale_listings,
            property_records=settings.rentcast_ttl_property_records,
            value_estimates=settings.rentcast_ttl_value_estimates,
        ),
    )
    loaded = 0
    try:
        source = RentCastListingSource(client)
        for spec in pack.sources.listings:
            if not isinstance(spec, RentCastListings) or not spec.enabled:
                continue
            batch = source.fetch_listings(listing_query(pack, spec))
            with engine.begin() as connection:
                loaded += upsert_listings(connection, pack.market.id, batch)
    finally:
        client.close()
    return loaded

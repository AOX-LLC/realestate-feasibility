import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import Engine, text

from feasibility.config import DataMode, Settings
from feasibility.domain import Address
from feasibility.jobs import queue
from feasibility.jobs.handlers import (
    JobContext,
    ListingsSyncPayload,
    build_registry,
    listing_query,
    run_listings_sync,
)
from feasibility.jobs.worker import Worker
from feasibility.listings import upsert_listings
from feasibility.markets.loader import get_pack
from feasibility.sources.base import ListingBatch
from feasibility.sources.rentcast import models
from feasibility.sources.rentcast.adapter import to_listing, to_value_estimate
from feasibility.sources.rentcast.client import RentCastClient, sale_listings_params
from feasibility.sources.rentcast.transport import BASE_URL, SnapshotTransport, request_key
from feasibility.sources.rentcast.verify import CallCeilingError, CeilingTransport, field_shapes

SENTINEL = "sentinel-sync-key-81b0"
LISTING = {
    "id": "100-Synthetic-Elm-St,-Dallas,-TX-75214",
    "formattedAddress": "100 Synthetic Elm St, Dallas, TX 75214",
    "addressLine1": "100 Synthetic Elm St",
    "city": "Dallas",
    "state": "TX",
    "zipCode": "75214",
    "price": 450000,
    "lotSize": 7405,
    "squareFootage": 1320,
    "yearBuilt": 1951,
    "listedDate": "2026-09-30T00:00:00.000Z",
    "listingAgent": {"name": "Agent Person", "email": "agent@example.com"},
    "someNewField": "kept",
}


def _settings(tmp_path: Path, mode: DataMode = DataMode.MOCK) -> Settings:
    key = SecretStr(SENTINEL) if mode is DataMode.LIVE else None
    return Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=mode, rentcast_api_key=key, snapshot_dir=tmp_path
    )


def _write_dallas_snapshot(directory: Path, body: Any) -> None:
    spec = get_pack("dallas").sources.listings[0]
    params = sale_listings_params(listing_query(get_pack("dallas"), spec))
    (directory / "rentcast").mkdir(exist_ok=True)
    snapshot_file = directory / "rentcast" / f"{request_key('/listings/sale', params)}.json"
    snapshot_file.write_text(json.dumps({"status": 200, "body": body}))


def test_listing_maps_to_the_domain_without_personal_fields() -> None:
    record = models.SaleListing.model_validate(
        {k: v for k, v in LISTING.items() if k != "listingAgent"}
    )

    listing = to_listing(record)

    assert listing.address.one_line == "100 SYNTHETIC ELM ST, DALLAS, TX 75214"
    assert listing.price == Decimal("450000")
    assert listing.remarks is None
    assert "someNewField" not in listing.raw
    assert "listingAgent" not in listing.raw


def test_value_estimate_keeps_only_sale_comps() -> None:
    comp = {"id": "c", "formattedAddress": "1 A St, Dallas, TX 75214", "price": 400000}
    estimate = models.ValueEstimate.model_validate(
        {
            "price": 420000,
            "comparables": [
                {**comp, "listingType": "Standard"},
                {**comp, "listingType": "New Construction"},
                {**comp, "price": 1900},
                {**comp, "listingType": "Standard", "price": None},
            ],
        }
    )

    value = to_value_estimate(estimate, Address(street="1 A ST"))

    assert len(value.comparables) == 2
    assert value.dropped_comparables == 2


def test_mock_sync_upserts_listings_and_keeps_first_seen(engine: Engine, tmp_path: Path) -> None:
    _write_dallas_snapshot(tmp_path, [LISTING])
    context = JobContext(engine=engine, settings=_settings(tmp_path), job_id=1)

    run_listings_sync(ListingsSyncPayload(market="dallas"), context)
    with engine.connect() as connection:
        first_seen = connection.execute(text("SELECT first_seen_at FROM listing")).scalar_one()
    run_listings_sync(ListingsSyncPayload(market="dallas"), context)

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM listing")).all()
    assert len(rows) == 1
    assert rows[0].first_seen_at == first_seen
    assert rows[0].zip5 == "75214"
    assert "Agent Person" not in json.dumps(rows[0].raw)


@respx.mock(base_url=BASE_URL)
def test_failed_live_sync_records_a_redacted_job_error(
    respx_mock: respx.MockRouter, engine: Engine, tmp_path: Path
) -> None:
    respx_mock.get("/listings/sale").respond(
        500, json={"status": 500, "error": "server-error", "message": f"key {SENTINEL}"}
    )
    settings = _settings(tmp_path, DataMode.LIVE)
    with engine.begin() as connection:
        job_id = queue.enqueue(connection, "listings.sync", ListingsSyncPayload(market="dallas"))

    assert Worker(engine, settings, build_registry(), worker_id="w").run_once()

    with engine.connect() as connection:
        last_error = connection.execute(
            text("SELECT last_error FROM job WHERE id = :id"), {"id": job_id}
        ).scalar_one()
    assert "RentCastError" in last_error
    assert SENTINEL not in last_error


def test_ceiling_transport_refuses_past_the_ceiling(tmp_path: Path) -> None:
    transport = CeilingTransport(SnapshotTransport(tmp_path), ceiling=2)
    transport.get("/a", {})
    transport.get("/b", {})

    with pytest.raises(CallCeilingError):
        transport.get("/c", {})
    assert transport.calls == 2


def test_field_shapes_report_types_never_values() -> None:
    shapes = field_shapes(
        [{"id": "secret-looking-value", "history": {"2026-09-30": {"price": 1.5}}, "hoa": None}]
    )

    assert shapes == {"[].id": "str", "[].history.*.price": "float", "[].hoa": "NoneType"}
    assert "secret-looking-value" not in json.dumps(shapes)


def _stored(engine: Engine) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT first_seen_at, last_seen_at, unit, price FROM listing")
        ).one()


def _batch(price: int = 450000, *, stale: bool = False, unit: str | None = None) -> Any:
    record = models.SaleListing.model_validate(
        {**LISTING, "price": price, "addressLine2": unit, "listingAgent": None}
    )
    return ListingBatch([to_listing(record)], stale=stale)


def test_observed_at_sets_both_stamps_on_insert(engine: Engine) -> None:
    seen = datetime(2026, 10, 1, 6, 0, tzinfo=ZoneInfo("America/Chicago"))

    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch(), seen)

    row = _stored(engine)
    assert row.first_seen_at == seen
    assert row.last_seen_at == seen


def test_a_later_observation_moves_only_last_seen_at(engine: Engine) -> None:
    first = datetime(2026, 10, 1, 6, 0, tzinfo=ZoneInfo("America/Chicago"))
    second = first + timedelta(days=1)

    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch(450000), first)
        upsert_listings(connection, "dallas", _batch(430000), second)

    row = _stored(engine)
    assert row.first_seen_at == first
    assert row.last_seen_at == second
    assert row.price == Decimal("430000")


def test_a_stale_batch_moves_neither_stamp(engine: Engine) -> None:
    first = datetime(2026, 10, 1, 6, 0, tzinfo=ZoneInfo("America/Chicago"))

    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch(), first)
        upsert_listings(connection, "dallas", _batch(stale=True), first + timedelta(days=1))

    row = _stored(engine)
    assert row.first_seen_at == first
    assert row.last_seen_at == first


def test_without_observed_at_the_stamps_are_the_clock(engine: Engine) -> None:
    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch())

    assert abs(_stored(engine).first_seen_at - datetime.now(UTC)) < timedelta(minutes=5)


def test_unit_round_trips_and_updates(engine: Engine) -> None:
    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch(unit="Unit 4B"))
    assert _stored(engine).unit == "UNIT 4B"

    with engine.begin() as connection:
        upsert_listings(connection, "dallas", _batch(unit=None))
    assert _stored(engine).unit is None


def test_snapshot_transport_prefers_the_overlay(tmp_path: Path) -> None:
    base, overlay = tmp_path / "base", tmp_path / "day-2"
    base.mkdir()
    overlay.mkdir()
    key = request_key("/a", {})
    only_base = request_key("/b", {})
    (base / f"{key}.json").write_text(json.dumps({"status": 200, "body": "base"}))
    (base / f"{only_base}.json").write_text(json.dumps({"status": 200, "body": "base-only"}))
    (overlay / f"{key}.json").write_text(json.dumps({"status": 200, "body": "overlay"}))

    transport = SnapshotTransport(base, overlay)

    assert transport.get("/a", {}).body == "overlay"
    assert transport.get("/b", {}).body == "base-only"
    assert transport.get("/c", {}).status_code == 404
    assert SnapshotTransport(base).get("/a", {}).body == "base"


def test_from_settings_rejects_a_snapshot_day_in_live_mode(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mock mode"):
        RentCastClient.from_settings(
            engine, _settings(tmp_path, DataMode.LIVE), snapshot_day="day-2"
        )

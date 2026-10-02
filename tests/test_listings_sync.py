import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from feasibility.markets.loader import get_pack
from feasibility.sources.rentcast import models
from feasibility.sources.rentcast.adapter import to_listing, to_value_estimate
from feasibility.sources.rentcast.client import sale_listings_params
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

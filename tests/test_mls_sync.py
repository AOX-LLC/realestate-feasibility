"""Remarks reach the listing table only redacted, through every path that syncs listings."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_api import READ_HEADERS, with_tokens

from feasibility.api.app import create_app
from feasibility.config import DataMode, Settings
from feasibility.jobs.handlers import JobContext, run_listings_sync
from feasibility.jobs.payloads import ListingsSyncPayload
from feasibility.listings import listing_query
from feasibility.markets.loader import get_pack
from feasibility.sources.mls.reso import DROPPED_FIELDS
from feasibility.sources.rentcast.client import sale_listings_params
from feasibility.sources.rentcast.transport import request_key

MLS_NUMBER = "SYN000777"
PLANTED = (
    "Dana Whitfield",
    "214.555.0187",
    "dana@example.com",
    "Whitfield Realty",
    "TREC #0123456",
)
RECORD: dict[str, Any] = {
    "ListingKey": "KEY-777",
    "ListingId": MLS_NUMBER,
    "UnparsedAddress": "100 Synthetic Elm St, Dallas, TX 75214",
    "PostalCode": "75214",
    "ListPrice": 450000,
    "StandardStatus": "Active",
    "PublicRemarks": (
        "Teardown candidate on a flat lot. Call Dana Whitfield 214.555.0187 or "
        "dana@example.com - sold as-is. Listed by Whitfield Realty, TREC #0123456."
    ),
    "ListAgentFullName": "Dana Whitfield",
    "ListAgentDirectPhone": "214-555-0187",
    "ListAgentEmail": "dana@example.com",
    "ListOfficeName": "Whitfield Realty",
    "ListOfficePhone": "214-555-0100",
    "PrivateRemarks": "Lockbox code 0000",
    "ShowingInstructions": "Call the listing agent",
}
FEED_LISTING = {
    "id": "100-Synthetic-Elm-St,-Dallas,-TX-75214",
    "formattedAddress": "100 Synthetic Elm St, Dallas, TX 75214",
    "addressLine1": "100 Synthetic Elm St",
    "city": "Dallas",
    "state": "TX",
    "zipCode": "75214",
    "price": 450000,
    "lotSize": 7405,
    "listedDate": "2026-09-30T00:00:00.000Z",
    "mlsName": "SYNTHETIC",
    "mlsNumber": MLS_NUMBER,
}
EXPECTED_REMARKS = (
    "Teardown candidate on a flat lot. [contact removed] - sold as-is. [contact removed]."
)


def _settings(tmp_path: Path, mode: DataMode = DataMode.MOCK) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=mode,
        snapshot_dir=tmp_path / "snapshot",
        mls_dir=tmp_path / "mls",
    )


def _write_feed(tmp_path: Path) -> None:
    pack = get_pack("dallas")
    params = sale_listings_params(listing_query(pack, pack.sources.listings[0]))
    directory = tmp_path / "snapshot" / "rentcast"
    directory.mkdir(parents=True)
    (directory / f"{request_key('/listings/sale', params)}.json").write_text(
        json.dumps({"status": 200, "body": [FEED_LISTING]})
    )


def _write_records(tmp_path: Path, records: list[dict[str, Any]]) -> None:
    (tmp_path / "mls").mkdir()
    (tmp_path / "mls" / "dallas.json").write_text(json.dumps(records))


def _sync(engine: Engine, tmp_path: Path) -> None:
    context = JobContext(engine=engine, settings=_settings(tmp_path), job_id=1)
    run_listings_sync(ListingsSyncPayload(market="dallas"), context)


def _row(engine: Engine) -> Any:
    with engine.connect() as connection:
        return connection.execute(text("SELECT remarks, raw::text AS raw FROM listing")).one()


def test_a_synced_listing_carries_the_redacted_remarks(engine: Engine, tmp_path: Path) -> None:
    _write_feed(tmp_path)
    _write_records(tmp_path, [RECORD])

    _sync(engine, tmp_path)

    assert _row(engine).remarks == EXPECTED_REMARKS


def test_no_planted_string_and_no_reso_key_reaches_the_table(
    engine: Engine, tmp_path: Path
) -> None:
    _write_feed(tmp_path)
    _write_records(tmp_path, [RECORD])

    _sync(engine, tmp_path)

    with engine.connect() as connection:
        everything = connection.execute(text("SELECT listing::text FROM listing")).scalar_one()
    for planted in (*PLANTED, "Lockbox", "listing agent"):
        assert planted not in everything
    for reso_field in (*DROPPED_FIELDS, "PublicRemarks", "ListingKey", "UnparsedAddress"):
        assert reso_field not in everything


def test_a_second_sync_keeps_the_remarks(engine: Engine, tmp_path: Path) -> None:
    """An upsert replaces a listing's remarks, so the listings.sync job must pass the source
    too, or a routine sync would erase what the daily run attached."""
    _write_feed(tmp_path)
    _write_records(tmp_path, [RECORD])

    _sync(engine, tmp_path)
    _sync(engine, tmp_path)

    assert _row(engine).remarks == EXPECTED_REMARKS


def test_a_listing_with_no_record_has_no_remarks(engine: Engine, tmp_path: Path) -> None:
    _write_feed(tmp_path)
    _write_records(tmp_path, [{**RECORD, "ListingId": "SYN000999"}])

    _sync(engine, tmp_path)

    assert _row(engine).remarks is None


def test_a_record_with_null_remarks_gives_a_listing_with_none(
    engine: Engine, tmp_path: Path
) -> None:
    _write_feed(tmp_path)
    _write_records(tmp_path, [{**RECORD, "PublicRemarks": None}])

    _sync(engine, tmp_path)

    assert _row(engine).remarks is None


def test_a_market_with_no_file_syncs_without_remarks(engine: Engine, tmp_path: Path) -> None:
    _write_feed(tmp_path)

    _sync(engine, tmp_path)

    assert _row(engine).remarks is None


def test_the_api_serves_the_redacted_remarks_and_nothing_planted(
    engine: Engine, tmp_path: Path
) -> None:
    _write_feed(tmp_path)
    _write_records(tmp_path, [RECORD])
    _sync(engine, tmp_path)

    with TestClient(
        create_app(with_tokens(_settings(tmp_path)), engine), headers=READ_HEADERS
    ) as client:
        listing_id = client.get("/listings").json()["items"][0]["id"]
        bodies = [client.get("/listings").text, client.get(f"/listings/{listing_id}").text]
        detail = client.get(f"/listings/{listing_id}").json()

    assert detail["remarks"] == EXPECTED_REMARKS
    for body in bodies:
        for planted in PLANTED:
            assert planted not in body

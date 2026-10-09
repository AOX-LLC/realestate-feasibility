"""Both snapshot days through the real sourcing run: what the database and the API hold of the
synthetic remarks."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from conftest import empty_database
from fastapi.testclient import TestClient
from mls_data import answer_key, feed_listings, feed_row_ids, ingested, snapshot_records
from sqlalchemy import Engine, text
from test_api import READ_HEADERS, with_tokens

from feasibility.api.app import create_app
from feasibility.config import DataMode, Settings
from feasibility.snapshot.load import seed
from feasibility.sources.mls.reso import DROPPED_FIELDS
from feasibility.sourcing.run import run_sourcing

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
RESO_KEYS = (*DROPPED_FIELDS, "PublicRemarks", "ListingKey", "ListingId", "UnparsedAddress")


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture(scope="module")
def both_days(migrated_engine: Engine) -> Iterator[Engine]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE)
    run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO)
    yield migrated_engine
    empty_database(migrated_engine)


def _stored(engine: Engine) -> dict[str, Any]:
    """Each stored listing's remarks and raw payload, by the feed's `mlsNumber`."""
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT remarks, raw FROM listing")).all()
    return {row.raw["mlsNumber"]: row for row in rows}


def _planted_strings() -> list[str]:
    return [
        planted
        for entry in answer_key().values()
        if "personal:residual" not in entry["tags"]
        for planted in entry["planted_personal"]
    ]


def test_every_listing_with_a_record_carries_its_redacted_remarks(both_days: Engine) -> None:
    stored = _stored(both_days)
    records = {record["ListingId"]: record for record in snapshot_records()}

    assert set(stored) == set(feed_listings())
    for mls_number, row in stored.items():
        found = ingested(records[mls_number])
        assert row.remarks == (None if found is None else found.text), mls_number


def test_the_count_of_listings_with_remarks_is_the_count_of_records_with_remarks(
    both_days: Engine,
) -> None:
    has_remarks = {r["ListingId"] for r in snapshot_records() if r["PublicRemarks"] is not None}
    # A relisted property is two rows with one record, and both carry its remarks.
    with_remarks = sum(1 for number in feed_row_ids().values() if number in has_remarks)

    with both_days.connect() as connection:
        stored = connection.execute(
            text("SELECT count(*) FROM listing WHERE remarks IS NOT NULL")
        ).scalar_one()

    assert stored == with_remarks


def test_no_stored_raw_payload_holds_a_reso_key_or_a_planted_string(both_days: Engine) -> None:
    with both_days.connect() as connection:
        dump = connection.execute(
            text("SELECT string_agg(listing::text, ' ') FROM listing")
        ).scalar_one()

    for key in RESO_KEYS:
        assert key not in dump, key
    for planted in _planted_strings():
        assert planted not in dump, planted


def test_the_api_serves_redacted_remarks_and_no_planted_string(both_days: Engine) -> None:
    planted = _planted_strings()
    bodies: list[str] = []
    served: dict[int, str | None] = {}
    with TestClient(
        create_app(with_tokens(_settings()), both_days), headers=READ_HEADERS
    ) as client:
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 50, **({"after": after} if after else {})}
            response = client.get("/listings", params=params)
            assert response.status_code == 200
            bodies.append(response.text)
            page = response.json()
            for item in page["items"]:
                served[item["id"]] = item["remarks"]
                detail = client.get(f"/listings/{item['id']}")
                bodies.append(detail.text)
                assert detail.json()["remarks"] == item["remarks"]
            after = page["next_after"]
            if after is None:
                break

    has_remarks = {r["ListingId"] for r in snapshot_records() if r["PublicRemarks"] is not None}
    assert len(served) == len(feed_row_ids())
    assert sum(1 for remarks in served.values() if remarks is not None) == sum(
        1 for number in feed_row_ids().values() if number in has_remarks
    )
    everything = "".join(bodies)
    assert "[contact removed]" in everything
    for string in planted:
        assert string not in everything, string

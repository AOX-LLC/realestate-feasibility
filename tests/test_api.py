from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from feasibility.api.app import create_app
from feasibility.config import DataMode, Settings
from feasibility.db import create_db_engine
from feasibility.sources.rentcast.client import PROVIDER
from feasibility.tables import api_budget, job, listing, parcel

SENTINEL = "sk-sentinel-7f3a9c1e5b"
HEALTH_KEYS = {
    "status",
    "commit",
    "commit_source",
    "branch",
    "version",
    "schema_version",
    "uptime_s",
    "mode",
    "rentcast_key_configured",
}
FILE_DATE = date(2026, 1, 15)


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


def _client(engine: Engine, settings: Settings | None = None) -> TestClient:
    app = create_app(settings or _settings(), engine)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with _client(engine) as test_client:
        yield test_client


def _insert_parcels(engine: Engine, *account_ids: str, **overrides: Any) -> None:
    rows = [
        {
            "market": "dallas",
            "account_id": account_id,
            "attrs_file_date": FILE_DATE,
            **overrides,
        }
        for account_id in account_ids
    ]
    with engine.begin() as connection:
        connection.execute(parcel.insert(), rows)


def _insert_listings(engine: Engine, count: int, **overrides: Any) -> None:
    rows = [
        {
            "source": "test",
            "external_id": f"ext-{number}",
            "market": "dallas",
            "address_line": f"{number} Main St",
            "raw": {"note": "RAW-MARKER-12345"},
            **overrides,
        }
        for number in range(count)
    ]
    with engine.begin() as connection:
        connection.execute(listing.insert(), rows)


def _walk_pages(client: TestClient, path: str, **params: Any) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    after: str | None = None
    while True:
        query = {**params, **({"after": after} if after else {})}
        response = client.get(path, params=query)
        assert response.status_code == 200
        body = response.json()
        pages.append(body["items"])
        after = body["next_after"]
        if after is None:
            return pages
        assert len(pages) < 20, "pagination did not terminate"


# 1. health


def test_health_reports_ok_in_mock_mode(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == HEALTH_KEYS
    assert body["status"] == "ok"
    assert body["schema_version"] == "0002"
    assert body["commit_source"] == "process_start"
    assert body["mode"] == "mock"
    assert body["rentcast_key_configured"] is False
    assert isinstance(body["uptime_s"], int)


# 2. the key never leaves the process


def test_sentinel_key_appears_in_no_response(engine: Engine) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SecretStr(SENTINEL)
    )
    _insert_parcels(engine, "99000000000000001")
    _insert_listings(engine, 1)
    with engine.begin() as connection:
        connection.execute(job.insert().values(kind="echo", payload={}))
    paths = [
        "/health",
        "/markets",
        "/markets/dallas",
        "/parcels",
        "/parcels/dallas/99000000000000001",
        "/listings",
        "/jobs",
        "/budget",
        "/nothing-here",
        "/parcels?limit=0",
    ]

    with _client(engine, settings) as live_client:
        health = live_client.get("/health").json()
        assert health["mode"] == "live"
        assert health["rentcast_key_configured"] is True
        for path in paths:
            response = live_client.get(path)
            assert SENTINEL not in response.text, path
            assert SENTINEL not in str(dict(response.headers)), path


# 3. database down


def test_health_degrades_when_database_is_unreachable() -> None:
    dead_engine = create_db_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
    try:
        with _client(dead_engine) as dead_client:
            response = dead_client.get("/health")
    finally:
        dead_engine.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["schema_version"] is None


# 4. parcel pagination


def test_parcels_keyset_pagination_visits_each_row_once(engine: Engine, client: TestClient) -> None:
    ids = [f"9900000000000000{number}" for number in range(1, 6)]
    _insert_parcels(engine, *reversed(ids))

    first = client.get("/parcels", params={"limit": 2}).json()
    assert [item["account_id"] for item in first["items"]] == ids[:2]
    assert first["next_after"] == ids[1]

    pages = _walk_pages(client, "/parcels", limit=2)
    seen = [item["account_id"] for page in pages for item in page]
    assert seen == ids
    assert [len(page) for page in pages] == [2, 2, 1]


def test_parcels_zip_filter(engine: Engine, client: TestClient) -> None:
    _insert_parcels(engine, "99000000000000001", "99000000000000002", zip5="75201")
    _insert_parcels(engine, "99000000000000003", zip5="75202")

    body = client.get("/parcels", params={"zip5": "75202"}).json()

    assert [item["account_id"] for item in body["items"]] == ["99000000000000003"]
    assert body["next_after"] is None


@pytest.mark.parametrize("limit", ["101", "0", "-1", "abc"])
def test_parcels_rejects_bad_limit(client: TestClient, limit: str) -> None:
    assert client.get("/parcels", params={"limit": limit}).status_code == 422


def test_parcels_default_limit_is_fifty(engine: Engine, client: TestClient) -> None:
    _insert_parcels(engine, *[f"98{number:015d}" for number in range(51)])

    body = client.get("/parcels").json()

    assert len(body["items"]) == 50
    assert body["next_after"] == body["items"][-1]["account_id"]
    assert len(client.get("/parcels", params={"limit": 100}).json()["items"]) == 51


# 5. single parcel


def test_parcel_detail_found_missing_and_malformed(engine: Engine, client: TestClient) -> None:
    _insert_parcels(engine, "99000000000000001", city="DALLAS")

    found = client.get("/parcels/dallas/99000000000000001")
    assert found.status_code == 200
    assert found.json()["city"] == "DALLAS"

    assert client.get("/parcels/dallas/99000000000000009").status_code == 404

    bad_value = "12-34;drop"
    rejected = client.get(f"/parcels/dallas/{bad_value}")
    assert rejected.status_code == 422
    assert bad_value not in rejected.text
    assert "drop" not in rejected.text


# 6. money


def test_money_serializes_as_decimal_strings(engine: Engine, client: TestClient) -> None:
    _insert_parcels(
        engine,
        "99000000000000001",
        land_value=Decimal("300000.00"),
        improvement_value=Decimal("1250.50"),
        total_value=Decimal("301250.50"),
        lot_size_sqft=Decimal("7500.00"),
    )

    body = client.get("/parcels/dallas/99000000000000001").json()

    assert body["land_value"] == "300000.00"
    assert body["improvement_value"] == "1250.50"
    assert body["total_value"] == "301250.50"
    assert body["lot_size_sqft"] == "7500.00"
    assert body["year_built"] is None


# 7. listings


def test_listings_paginate_by_id_and_hide_raw(engine: Engine, client: TestClient) -> None:
    _insert_listings(engine, 5, price=Decimal("450000.00"))

    pages = _walk_pages(client, "/listings", limit=2)

    items = [item for page in pages for item in page]
    ids = [item["id"] for item in items]
    assert len(items) == 5
    assert ids == sorted(set(ids))
    assert [len(page) for page in pages] == [2, 2, 1]
    assert all("raw" not in item for item in items)
    assert all(item["remarks"] is None for item in items)
    assert items[0]["price"] == "450000.00"


def test_listings_response_never_contains_raw_text(engine: Engine, client: TestClient) -> None:
    _insert_listings(engine, 2)

    listing_id = client.get("/listings").json()["items"][0]["id"]
    for path in ("/listings", f"/listings/{listing_id}"):
        text = client.get(path).text
        assert "RAW-MARKER-12345" not in text


def test_listing_detail_missing_is_404(client: TestClient) -> None:
    assert client.get("/listings/424242").status_code == 404
    assert client.get("/listings/0").status_code == 422


# 8. jobs


def _insert_job(engine: Engine, status: str, last_error: str | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(
            job.insert().values(
                kind="echo",
                payload={"secret": "PAYLOAD-MARKER-24680"},
                status=status,
                last_error=last_error,
            )
        )


def test_jobs_newest_first_with_status_filter_and_error_flag(
    engine: Engine, client: TestClient
) -> None:
    _insert_job(engine, "done")
    _insert_job(engine, "failed", last_error="ERROR-MARKER-13579 traceback")
    _insert_job(engine, "queued")

    everything = client.get("/jobs").json()["items"]
    assert [item["status"] for item in everything] == ["queued", "failed", "done"]
    assert [item["has_error"] for item in everything] == [False, True, False]

    failed = client.get("/jobs", params={"status": "failed"}).json()["items"]
    assert [item["status"] for item in failed] == ["failed"]

    assert client.get("/jobs", params={"status": "bogus"}).status_code == 422


def test_jobs_never_expose_error_text_or_payload(engine: Engine, client: TestClient) -> None:
    _insert_job(engine, "failed", last_error="ERROR-MARKER-13579 traceback")

    response = client.get("/jobs")

    assert "ERROR-MARKER-13579" not in response.text
    assert "PAYLOAD-MARKER-24680" not in response.text
    item = response.json()["items"][0]
    assert "last_error" not in item
    assert "payload" not in item


def test_jobs_paginate_newest_first(engine: Engine, client: TestClient) -> None:
    for _ in range(3):
        _insert_job(engine, "done")

    pages = _walk_pages(client, "/jobs", limit=2)

    ids = [item["id"] for page in pages for item in page]
    assert len(ids) == 3
    assert ids == sorted(ids, reverse=True)


# 9. budget


def test_budget_in_mock_mode_starts_unspent(client: TestClient) -> None:
    body = client.get("/budget").json()

    assert body["provider"] == PROVIDER
    assert body["mode"] == "mock"
    assert (body["limit"], body["used"], body["remaining"]) == (50, 0, 50)


def test_budget_reports_usage_for_the_current_period(engine: Engine, client: TestClient) -> None:
    today = datetime.now(UTC).date()
    with engine.begin() as connection:
        connection.execute(
            api_budget.insert().values(
                provider=PROVIDER,
                period_start=today.replace(day=1),
                request_limit=50,
                used=7,
            )
        )

    body = client.get("/budget").json()

    assert body["period_start"] == today.replace(day=1).isoformat()
    assert (body["used"], body["remaining"]) == (7, 43)


# 10. markets


def test_markets_list_and_detail(client: TestClient) -> None:
    listed = client.get("/markets")
    assert listed.status_code == 200
    assert "dallas" in [market["id"] for market in listed.json()]

    detail = client.get("/markets/dallas")
    assert detail.status_code == 200
    buy_box = detail.json()["buy_box"]
    assert buy_box["zips"]
    assert all(len(zip5) == 5 for zip5 in buy_box["zips"])
    sourcing = detail.json()["sourcing"]
    assert sourcing["source_priority"] == ["mls", "rentcast"]
    assert sourcing["scoring"]["land_ratio_weight"] == "35"


def test_unknown_market_is_404_and_malformed_is_422(client: TestClient) -> None:
    assert client.get("/markets/nope").status_code == 404
    assert client.get("/markets/BAD!").status_code == 422


# 11. headers


def test_security_headers_are_set(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_security_headers_are_set_on_errors(client: TestClient) -> None:
    for path in ("/markets/nope", "/parcels?limit=0"):
        assert client.get(path).headers["X-Content-Type-Options"] == "nosniff"


# 12. internal errors


def test_internal_error_is_opaque(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> None:
        raise RuntimeError("boom sentinel")

    monkeypatch.setattr("feasibility.api.routes.markets.load_registry", explode)

    with _client(engine) as failing_client:
        response = failing_client.get("/markets")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert "boom" not in response.text


def test_ids_beyond_bigint_are_rejected_not_crashed(engine: Engine) -> None:
    client = _client(engine)
    huge = "99999999999999999999999"

    for path in (f"/jobs?after={huge}", f"/listings?after={huge}", f"/listings/{huge}"):
        response = client.get(path)
        assert response.status_code == 422, path
        assert huge not in response.text


def test_internal_errors_carry_the_security_headers(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("feasibility.api.routes.markets.load_registry", explode)

    response = _client(engine).get("/markets")

    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"

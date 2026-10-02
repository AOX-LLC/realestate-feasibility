import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import Engine, text

from feasibility.sources.base import ListingQuery
from feasibility.sources.rentcast import budget
from feasibility.sources.rentcast.client import (
    BudgetExhaustedError,
    RentCastClient,
    RentCastError,
    SchemaDriftError,
    Ttls,
)
from feasibility.sources.rentcast.scrub import scrub
from feasibility.sources.rentcast.transport import (
    BASE_URL,
    HttpTransport,
    SnapshotTransport,
    canonical_params,
    request_key,
)

SENTINEL = "sentinel-rentcast-key-5d2e"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
QUERY = ListingQuery(city="Dallas", state="TX", days_old=2, limit=500)
TTLS = Ttls(timedelta(hours=20), timedelta(days=30), timedelta(days=7))
LISTING = {
    "id": "100-Synthetic-Elm-St,-Dallas,-TX-75214",
    "formattedAddress": "100 Synthetic Elm St, Dallas, TX 75214",
    "price": 450000,
    "status": "Active",
    "listingAgent": {"name": "Agent Person", "phone": "5550100", "email": "agent@example.com"},
    "listingOffice": {"name": "Office Name", "phone": "5550101"},
}


def _client(engine: Engine, *, monthly_budget: int = 50, use_cache: bool = True) -> RentCastClient:
    return RentCastClient(
        engine,
        HttpTransport(SecretStr(SENTINEL)),
        live=True,
        monthly_budget=monthly_budget,
        billing_anchor_day=1,
        ttls=TTLS,
        use_cache=use_cache,
        clock=lambda: NOW,
    )


def _rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))  # noqa: S608
        return [dict(row._mapping) for row in result]


def _used(engine: Engine) -> int:
    with engine.connect() as connection:
        return budget.usage(connection, "rentcast", date(2026, 10, 1), 50).used


def _outcomes(engine: Engine) -> list[tuple[str, bool]]:
    return [(row["outcome"], row["billed"]) for row in _rows(engine, "api_request_log")]


def _expire_cache(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("UPDATE api_cache SET expires_at = now() - interval '1 second'"))


@respx.mock(base_url=BASE_URL)
def test_success_is_billed_cached_and_sent_with_the_header(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    route = respx_mock.get("/listings/sale").respond(200, json=[LISTING])
    client = _client(engine)

    first = client.sale_listings(QUERY)
    second = client.sale_listings(QUERY)

    assert [listing.id for listing in first.data] == [LISTING["id"]]
    assert second.data == first.data
    assert route.call_count == 1
    assert route.calls.last.request.headers["X-Api-Key"] == SENTINEL
    assert route.calls.last.request.url.params["daysOld"] == "2"
    assert _used(engine) == 1
    assert _outcomes(engine) == [("ok", True), ("cache_hit", False)]


@respx.mock(base_url=BASE_URL)
def test_personal_fields_never_reach_the_cache(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    respx_mock.get("/listings/sale").respond(200, json=[LISTING])

    listing = _client(engine).sale_listings(QUERY).data[0]

    cached = json.dumps([row["body"] for row in _rows(engine, "api_cache")])
    for marker in ("listingAgent", "listingOffice", "Agent Person", "agent@example.com"):
        assert marker not in cached
        assert marker not in listing.model_dump_json(by_alias=True)


def test_scrub_removes_personal_fields_at_any_depth() -> None:
    body = {"comparables": [LISTING], "owner": {"names": ["A Person"]}, "price": 1}

    assert scrub(body) == {
        "comparables": [{k: v for k, v in LISTING.items() if not k.startswith("listing")}],
        "price": 1,
    }


@respx.mock(base_url=BASE_URL)
def test_not_found_is_refunded_and_means_no_records(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    respx_mock.get("/listings/sale").respond(404, json={"status": 404, "error": "not-found"})

    assert _client(engine).sale_listings(QUERY).data == []
    assert _used(engine) == 0
    assert _outcomes(engine) == [("not_found", False)]
    assert _rows(engine, "api_cache") == []


@pytest.mark.parametrize("status", [400, 401, 429, 500, 504])
@respx.mock(base_url=BASE_URL)
def test_http_errors_are_refunded_and_never_cached(
    respx_mock: respx.MockRouter, engine: Engine, status: int
) -> None:
    respx_mock.get("/listings/sale").respond(
        status, json={"status": status, "error": "some/code", "message": f"echo {SENTINEL}"}
    )

    with pytest.raises(RentCastError) as raised:
        _client(engine).sale_listings(QUERY)

    assert raised.value.status == status
    assert raised.value.error_code == "some/code"
    assert _used(engine) == 0
    assert _rows(engine, "api_cache") == []
    assert _outcomes(engine) == [("http_error", False)]


@respx.mock(base_url=BASE_URL)
def test_an_error_serves_the_stale_entry_when_there_is_one(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    route = respx_mock.get("/listings/sale")
    route.respond(200, json=[LISTING])
    client = _client(engine)
    client.sale_listings(QUERY)
    _expire_cache(engine)
    route.respond(500, json={"status": 500, "error": "server-error"})

    fetched = client.sale_listings(QUERY)

    assert fetched.stale
    assert fetched.data[0].id == LISTING["id"]
    assert _outcomes(engine)[-2:] == [("http_error", False), ("stale_served", False)]


@respx.mock(base_url=BASE_URL)
def test_schema_drift_is_billed_logged_and_not_cached(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    respx_mock.get("/listings/sale").respond(200, json=[{"formattedAddress": "no id"}])

    with pytest.raises(SchemaDriftError) as raised:
        _client(engine).sale_listings(QUERY)

    assert raised.value.field_paths == ["0.id"]
    assert _used(engine) == 1
    assert _rows(engine, "api_cache") == []
    assert _outcomes(engine) == [("schema_error", True)]


@respx.mock(base_url=BASE_URL)
def test_connect_error_is_refunded(respx_mock: respx.MockRouter, engine: Engine) -> None:
    respx_mock.get("/listings/sale").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(RentCastError, match="connect_failed"):
        _client(engine).sale_listings(QUERY)

    assert _used(engine) == 0
    assert _outcomes(engine) == [("network_error", False)]


@respx.mock(base_url=BASE_URL)
def test_read_timeout_keeps_the_unit_spent(respx_mock: respx.MockRouter, engine: Engine) -> None:
    respx_mock.get("/listings/sale").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(RentCastError, match="no_response"):
        _client(engine).sale_listings(QUERY)

    assert _used(engine) == 1
    assert _outcomes(engine) == [("network_error", True)]


@respx.mock(base_url=BASE_URL)
def test_exhausted_budget_refuses_before_the_network(
    respx_mock: respx.MockRouter, engine: Engine
) -> None:
    route = respx_mock.get("/avm/value").respond(200, json={"price": 400000})
    client = _client(engine, monthly_budget=1)

    client.value_estimate("100 Synthetic Elm St, Dallas, TX 75214")
    with pytest.raises(BudgetExhaustedError):
        client.value_estimate("200 Synthetic Oak Ave, Dallas, TX 75214")

    assert route.call_count == 1
    assert _outcomes(engine)[-1] == ("refused_budget", False)


@respx.mock(base_url=BASE_URL)
def test_the_api_key_never_leaks(
    respx_mock: respx.MockRouter, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error_body = {"status": 0, "error": "x", "message": f"key {SENTINEL} rejected"}
    outcomes = [
        httpx.Response(200, json=[LISTING]),
        httpx.Response(401, json={**error_body, "status": 401}),
        httpx.Response(404, json={**error_body, "status": 404}),
        httpx.Response(429, json={**error_body, "status": 429}),
        httpx.Response(500, json={**error_body, "status": 500}),
        httpx.ConnectError(f"connect {SENTINEL}"),
        httpx.ReadTimeout(f"timeout {SENTINEL}"),
    ]
    respx_mock.get("/listings/sale").mock(side_effect=outcomes)
    client = _client(engine, use_cache=False)
    raised: list[str] = []

    for _ in outcomes:
        try:
            client.sale_listings(QUERY)
        except Exception as error:
            raised.append(f"{error!r} {error} {error.__cause__!r} {error.__context__!r}")

    assert len(raised) == 5  # every outcome except 200 and 404
    assert SENTINEL not in caplog.text
    assert SENTINEL not in " ".join(raised)
    for table in ("api_cache", "api_request_log", "api_budget"):
        assert SENTINEL not in json.dumps(_rows(engine, table), default=str)


def test_request_key_is_order_independent_and_keyless() -> None:
    forward = canonical_params({"city": "Dallas", "state": "TX", "limit": 5})
    backward = canonical_params({"limit": "5", "state": "TX ", "city": "Dallas"})

    assert forward == backward
    assert request_key("/listings/sale", forward) == request_key("/listings/sale", backward)


@pytest.mark.parametrize(
    ("today", "anchor", "expected"),
    [
        (date(2026, 10, 2), 1, date(2026, 10, 1)),
        (date(2026, 10, 2), 15, date(2026, 9, 15)),
        (date(2026, 1, 5), 20, date(2025, 12, 20)),
        (date(2026, 10, 15), 15, date(2026, 10, 15)),
    ],
)
def test_billing_period_start(today: date, anchor: int, expected: date) -> None:
    assert budget.period_start(today, anchor) == expected


def test_mock_mode_reads_the_snapshot_and_never_spends(engine: Engine, tmp_path: Path) -> None:
    params = canonical_params(
        {"city": "Dallas", "state": "TX", "status": "Active", "daysOld": "2", "limit": "500"}
    )
    snapshot_file = tmp_path / f"{request_key('/listings/sale', params)}.json"
    snapshot_file.write_text(json.dumps({"status": 200, "body": [LISTING]}))
    client = RentCastClient(
        engine,
        SnapshotTransport(tmp_path),
        live=False,
        monthly_budget=0,
        billing_anchor_day=1,
        ttls=TTLS,
    )

    listings = client.sale_listings(QUERY).data

    assert [listing.id for listing in listings] == [LISTING["id"]]
    assert client.value_estimate("not in the snapshot").data is None
    assert _rows(engine, "api_budget") == []
    assert _rows(engine, "api_cache") == []

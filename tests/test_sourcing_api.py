from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_api import SENTINEL, _client, _insert_listings, _walk_pages

from feasibility.api.app import create_app
from feasibility.api.routes import sourcing as sourcing_routes
from feasibility.config import DataMode, Settings
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import candidate, listing, run_candidate, run_listing, sourcing_run

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
MOCK = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture
def ran_both_days(engine: Engine) -> Iterator[TestClient]:
    """The synthetic snapshot sourced for both days; run 2 is the one the tests read."""
    seed(engine, MOCK)
    run_sourcing(engine, MOCK, "dallas", DAY_ONE)
    run_sourcing(engine, MOCK, "dallas", DAY_TWO)
    with _client(engine) as client:
        yield client


def _run_id(client: TestClient, as_of: str) -> int:
    runs = client.get("/sourcing/runs").json()["items"]
    return next(run["id"] for run in runs if run["as_of"] == as_of)


# 1. runs


def test_runs_are_listed_newest_first_with_their_counts(ran_both_days: TestClient) -> None:
    body = ran_both_days.get("/sourcing/runs").json()

    assert set(body) == {"items", "next_after"}
    assert body["next_after"] is None
    assert [run["as_of"] for run in body["items"]] == ["2026-10-02", "2026-10-01"]
    newest = body["items"][0]
    assert set(newest) == {
        "id",
        "market",
        "as_of",
        "status",
        "sync_status",
        "counts",
        "started_at",
        "finished_at",
    }
    assert (newest["status"], newest["sync_status"]) == ("completed", "fresh")
    assert newest["counts"]["ranked"] == 17
    assert newest["counts"]["match_rate"] == "0.9048"


def test_runs_page_by_descending_id(ran_both_days: TestClient) -> None:
    pages = _walk_pages(ran_both_days, "/sourcing/runs", limit=1)
    assert [[run["as_of"] for run in page] for page in pages] == [["2026-10-02"], ["2026-10-01"]]


def test_one_run_by_id(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    assert ran_both_days.get(f"/sourcing/runs/{run_id}").json()["id"] == run_id


# 2. candidates


def test_every_ranked_candidate_is_reached_once_in_rank_order(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    pages = _walk_pages(ran_both_days, f"/sourcing/runs/{run_id}/candidates", limit=5)

    assert [len(page) for page in pages] == [5, 5, 5, 2]
    ranks = [item["rank"] for page in pages for item in page]
    assert ranks == list(range(1, 18))
    first = pages[0][0]
    assert first["rank"] == 1 and first["score"] == "89.40" and first["price"] == "378000.00"
    assert first["status"] == "ranked" and first["match"]["method"] == "exact"
    assert set(first) == {
        "candidate_id",
        "rank",
        "score",
        "price",
        "address",
        "change_kind",
        "status",
        "filter_reasons",
        "unscored_reason",
        "match",
        "account_id",
    }


def test_the_unscored_list_is_the_two_day_two_rows(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    pages = _walk_pages(ran_both_days, f"/sourcing/runs/{run_id}/candidates", status="unscored")

    items = [item for page in pages for item in page]
    assert sorted((i["address"]["street"], i["unscored_reason"]) for i in items) == [
        ("2200 KESTRELWYN DR", "ambiguous"),
        ("9100 BRINDLECOMBE ST", "unmatched"),
    ]
    assert all(item["rank"] is None and item["score"] is None for item in items)
    assert [i["candidate_id"] for i in items] == sorted(i["candidate_id"] for i in items)


def test_filtered_candidates_carry_their_reasons(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    items = ran_both_days.get(
        f"/sourcing/runs/{run_id}/candidates", params={"status": "filtered"}
    ).json()["items"]
    assert [item["filter_reasons"] for item in items] == [["year_built", "land_to_total"]] * 2


# 3. detail


def test_detail_components_sum_to_the_total(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    first = ran_both_days.get(f"/sourcing/runs/{run_id}/candidates").json()["items"][0]

    detail = ran_both_days.get(f"/sourcing/runs/{run_id}/candidates/{first['candidate_id']}").json()

    breakdown = detail["breakdown"]
    assert breakdown["version"] == 1
    assert [c["name"] for c in breakdown["components"]] == [
        "land_ratio",
        "age",
        "lot",
        "price_vs_land",
    ]
    assert sum(Decimal(c["points"]) for c in breakdown["components"]) == Decimal(breakdown["total"])
    assert breakdown["total"] == detail["score"] == "89.40"
    assert [(i["is_primary"], i["change_kind"]) for i in detail["listings"]] == [
        (True, "unchanged")
    ]


def test_a_relisted_candidate_shows_the_listing_that_left_and_the_one_that_returned(
    ran_both_days: TestClient,
) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    items = ran_both_days.get(f"/sourcing/runs/{run_id}/candidates", params={"limit": 100}).json()[
        "items"
    ]
    relisted = next(item for item in items if item["change_kind"] == "relisted")

    detail = ran_both_days.get(
        f"/sourcing/runs/{run_id}/candidates/{relisted['candidate_id']}"
    ).json()

    assert sorted((i["change_kind"], i["is_primary"]) for i in detail["listings"]) == [
        ("gone", False),
        ("relisted", True),
    ]


def test_breakdown_is_null_for_filtered_and_unscored(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    for status in ("filtered", "unscored"):
        item = ran_both_days.get(
            f"/sourcing/runs/{run_id}/candidates", params={"status": status}
        ).json()["items"][0]
        detail = ran_both_days.get(f"/sourcing/runs/{run_id}/candidates/{item['candidate_id']}")
        assert detail.json()["breakdown"] is None


# 4. errors


@pytest.mark.parametrize(
    "path",
    [
        "/sourcing/runs/999999",
        "/sourcing/runs/999999/candidates",
        "/sourcing/runs/999999/candidates/1",
    ],
)
def test_unknown_runs_are_404(ran_both_days: TestClient, path: str) -> None:
    assert ran_both_days.get(path).status_code == 404


def test_a_candidate_that_is_not_in_the_run_is_404(ran_both_days: TestClient) -> None:
    run_id = _run_id(ran_both_days, "2026-10-02")
    assert ran_both_days.get(f"/sourcing/runs/{run_id}/candidates/999999").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/sourcing/runs/1/candidates?status=bogus",
        "/sourcing/runs/1/candidates?limit=0",
        "/sourcing/runs/1/candidates?limit=101",
        "/sourcing/runs/0",
        "/sourcing/runs/abc",
        f"/sourcing/runs/{2**63}",
        f"/sourcing/runs/1/candidates/{2**63}",
        "/sourcing/runs?market=Not%20A%20Market",
    ],
)
def test_bad_input_is_a_422_that_does_not_echo_it(ran_both_days: TestClient, path: str) -> None:
    response = ran_both_days.get(path)
    assert response.status_code == 422
    assert "bogus" not in response.text


# 5. read-only


def test_every_route_is_get_only(engine: Engine) -> None:
    """Read from the OpenAPI document: included routers are not plain APIRoutes."""
    paths = create_app(MOCK, engine).openapi()["paths"]
    sourcing = [path for path in paths if path.startswith("/sourcing")]

    assert len(sourcing) == 4
    assert all(set(methods) == {"get"} for methods in paths.values())


def test_the_router_module_cannot_write_or_spend() -> None:
    for name in ("RentCastClient", "run_sourcing", "enqueue_job", "enqueue", "sync_listings"):
        assert not hasattr(sourcing_routes, name), name


def test_no_response_leaks_owner_raw_or_the_key(engine: Engine) -> None:
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SENTINEL
    )
    seed(engine, MOCK)
    run_sourcing(engine, MOCK, "dallas", DAY_ONE)
    with _client(engine, live) as client:
        run_id = _run_id(client, "2026-10-01")
        top = client.get(f"/sourcing/runs/{run_id}/candidates").json()["items"][0]
        paths = [
            "/sourcing/runs",
            f"/sourcing/runs/{run_id}",
            f"/sourcing/runs/{run_id}/candidates",
            f"/sourcing/runs/{run_id}/candidates/{top['candidate_id']}",
        ]
        for path in paths:
            text = client.get(path).text.lower()
            assert "owner" not in text and "raw" not in text, path
            assert SENTINEL.lower() not in text, path


# 6. rows inserted directly


def _insert_run(engine: Engine, as_of: date, counts: dict[str, Any] | None = None) -> int:
    with engine.begin() as connection:
        return connection.execute(
            sourcing_run.insert()
            .values(
                market="dallas",
                as_of=as_of,
                status="running",
                sync_status="pending",
                counts=counts or {},
                started_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
            .returning(sourcing_run.c.id)
        ).scalar_one()


def test_a_running_run_with_no_counts_yet_still_serves(engine: Engine) -> None:
    run_id = _insert_run(engine, DAY_ONE)
    with _client(engine) as client:
        body = client.get(f"/sourcing/runs/{run_id}").json()
    assert body["status"] == "running"
    assert body["counts"]["ranked"] == 0
    assert body["counts"]["match_rate"] is None


def test_a_filtered_candidate_inserted_by_hand_has_no_breakdown(engine: Engine) -> None:
    _insert_listings(engine, 1, price=Decimal(300000), zip5="75214")
    run_id = _insert_run(engine, DAY_ONE)
    with engine.begin() as connection:
        listing_id = connection.execute(select(listing.c.id)).scalar_one()
        candidate_id = connection.execute(
            candidate.insert()
            .values(
                market="dallas",
                property_key="acct:1",
                account_id="1",
                street_key="0|0|MAIN ST",
                first_as_of=DAY_ONE,
            )
            .returning(candidate.c.id)
        ).scalar_one()
        connection.execute(
            run_listing.insert().values(
                run_id=run_id,
                listing_id=listing_id,
                change_kind="new",
                price=Decimal(300000),
                candidate_id=candidate_id,
                is_primary=True,
            )
        )
        connection.execute(
            run_candidate.insert().values(
                run_id=run_id,
                candidate_id=candidate_id,
                primary_listing_id=listing_id,
                change_kind="new",
                status="filtered",
                filter_reasons=["lot_size"],
            )
        )
    with _client(engine) as client:
        body = client.get(f"/sourcing/runs/{run_id}/candidates/{candidate_id}").json()
    assert body["status"] == "filtered" and body["breakdown"] is None
    assert body["match"] is None

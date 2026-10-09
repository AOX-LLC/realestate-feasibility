"""The read-only pro-forma endpoints over both snapshot days. Run 2 is the one read here."""

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from conftest import empty_database
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_api import SENTINEL, _client, _walk_pages

from feasibility.api.app import create_app
from feasibility.api.routes import proforma as proforma_routes
from feasibility.config import DataMode, Settings
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import proforma, run_candidate

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
MOCK = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
PERSONAL_DATA_KEY = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.I)
SUMMARY_KEYS = {
    "candidate_id",
    "rank",
    "address",
    "status",
    "reason",
    "flags",
    "offer_price",
    "arv",
    "total_cost",
    "profit",
    "margin",
    "roi",
    "annualized_return",
    "max_offer",
}


@pytest.fixture(scope="module")
def run_two(migrated_engine: Engine) -> Iterator[tuple[TestClient, int]]:
    """The client and the id of day two's run. Built once: nothing here writes through the
    API, and this module's tests never use the function-scoped `engine` fixture."""
    empty_database(migrated_engine)
    seed(migrated_engine, MOCK)
    run_sourcing(migrated_engine, MOCK, "dallas", DAY_ONE)
    run_sourcing(migrated_engine, MOCK, "dallas", DAY_TWO)
    with _client(migrated_engine) as client:
        runs = client.get("/sourcing/runs").json()["items"]
        yield client, next(run["id"] for run in runs if run["as_of"] == "2026-10-02")


def _list(client: TestClient, run_id: int, **params: Any) -> list[dict[str, Any]]:
    pages = _walk_pages(client, f"/sourcing/runs/{run_id}/proformas", **params)
    return [item for page in pages for item in page]


def test_the_list_walks_every_rank_once_in_pages_of_five(
    run_two: tuple[TestClient, int],
) -> None:
    client, run_id = run_two
    pages = _walk_pages(client, f"/sourcing/runs/{run_id}/proformas", limit=5)

    assert [len(page) for page in pages] == [5, 5, 5, 2]
    ranks = [item["rank"] for page in pages for item in page]
    assert ranks == list(range(1, 18))
    first = client.get(f"/sourcing/runs/{run_id}/proformas", params={"limit": 5}).json()
    assert first["next_after"] == "5"
    last = client.get(f"/sourcing/runs/{run_id}/proformas", params={"after": 15, "limit": 5}).json()
    assert [item["rank"] for item in last["items"]] == [16, 17]
    assert last["next_after"] is None
    beyond = client.get(f"/sourcing/runs/{run_id}/proformas", params={"after": 17}).json()
    assert beyond == {"items": [], "next_after": None}


def test_a_status_filter_returns_only_that_status(run_two: tuple[TestClient, int]) -> None:
    client, run_id = run_two

    computed = _list(client, run_id, status="computed")
    no_arv = _list(client, run_id, status="no_arv")
    unsizable = _list(client, run_id, status="unsizable")

    # 006 is ranked sixth on day two and still has day one's estimate, so six compute.
    assert [item["rank"] for item in computed] == [1, 2, 3, 4, 5, 6]
    assert {item["status"] for item in computed} == {"computed"}
    assert len(no_arv) == 11 and {item["status"] for item in no_arv} == {"no_arv"}
    assert unsizable == []


def test_a_list_item_has_the_documented_shape_and_decimal_strings(
    run_two: tuple[TestClient, int],
) -> None:
    client, run_id = run_two
    items = _list(client, run_id)

    assert all(set(item) == SUMMARY_KEYS for item in items)
    top = items[0]
    assert top["address"] == {"street": "8773 ORRINMOOR TRL", "zip5": "75209"}
    assert (top["status"], top["reason"]) == ("computed", None)
    assert top["flags"] == ["size_capped_max", "vacant_lot"]
    assert (top["offer_price"], top["arv"]) == ("378000.00", "1678067.65")
    assert (top["total_cost"], top["profit"], top["margin"]) == (
        "1348504.58",
        "329563.07",
        "0.1964",
    )
    unpriced = next(item for item in items if item["status"] == "no_arv")
    assert unpriced["reason"] == "no_estimate_yet"
    for name in ("arv", "total_cost", "profit", "margin", "roi", "annualized_return", "max_offer"):
        assert unpriced[name] is None
    assert unpriced["offer_price"] is not None


def _detail(client: TestClient, run_id: int, rank: int) -> dict[str, Any]:
    item = next(item for item in _list(client, run_id) if item["rank"] == rank)
    response = client.get(f"/sourcing/runs/{run_id}/candidates/{item['candidate_id']}/proforma")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_a_computed_detail_adds_up(run_two: tuple[TestClient, int]) -> None:
    client, run_id = run_two
    detail = _detail(client, run_id, 2)  # acct 004, the price cut
    result = detail["result"]
    costs, financing, holding, selling = (
        result[name] for name in ("costs", "financing", "holding", "selling")
    )
    totals = result["totals"]

    assert {name: detail[name] for name in SUMMARY_KEYS} == next(
        item for item in _list(client, run_id) if item["rank"] == 2
    )
    lines = (
        Decimal(costs["offer_price"])
        + Decimal(costs["acquisition_closing"])
        + Decimal(costs["demolition"])
        + Decimal(costs["hard_cost"])
        + Decimal(costs["contingency"])
        + Decimal(costs["soft_costs"])
        + Decimal(financing["total"])
        + Decimal(holding["total"])
        + Decimal(selling["total"])
    )
    assert lines == Decimal(totals["total_cost"]) == Decimal(detail["total_cost"])
    assert Decimal(result["arv"]["arv"]) - lines == Decimal(totals["profit"])
    assert detail["offer_price"] == "321000.00"
    assert result["version"] == 1 and result["assumptions"]["status"] == "illustrative"
    cells = result["sensitivity"]["cells"]
    assert len(cells) == 60
    centre = next(
        cell
        for cell in cells
        if (cell["arv_delta_pct"], cell["hard_cost_delta_pct"], cell["hold_months"])
        == ("0", "0", "9")
    )
    assert (centre["profit"], centre["total_cost"]) == (totals["profit"], totals["total_cost"])


def test_a_no_arv_detail_keeps_the_costs_that_need_no_arv(
    run_two: tuple[TestClient, int],
) -> None:
    client, run_id = run_two
    result = _detail(client, run_id, 7)["result"]  # the two-account lot, outside the line

    assert (result["status"], result["reason"]) == ("no_arv", "no_estimate_yet")
    assert "gis_group" in result["flags"]
    for kept in ("site", "sizing", "costs", "financing", "holding"):
        assert result[kept] is not None, kept
    for missing in ("arv", "selling", "totals", "max_offer", "sensitivity"):
        assert result[missing] is None, missing


@pytest.mark.parametrize("which", ["list", "detail"])
def test_unknown_runs_and_candidates_are_404(run_two: tuple[TestClient, int], which: str) -> None:
    client, run_id = run_two
    ranked = _list(client, run_id)[0]["candidate_id"]
    paths = {
        "list": ["/sourcing/runs/999999/proformas"],
        "detail": [
            f"/sourcing/runs/999999/candidates/{ranked}/proforma",
            f"/sourcing/runs/{run_id}/candidates/999999/proforma",
        ],
    }[which]

    for path in paths:
        assert client.get(path).status_code == 404, path
    if which == "detail":
        # An unknown run says so, as the list does.
        assert client.get(paths[0]).json()["detail"] == "no such run"


def test_a_candidate_the_run_did_not_rank_has_no_pro_forma(
    run_two: tuple[TestClient, int], migrated_engine: Engine
) -> None:
    client, run_id = run_two
    with migrated_engine.connect() as connection:
        filtered = connection.execute(
            select(run_candidate.c.candidate_id)
            .where(run_candidate.c.run_id == run_id, run_candidate.c.status != "ranked")
            .limit(1)
        ).scalar_one()

    response = client.get(f"/sourcing/runs/{run_id}/candidates/{filtered}/proforma")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/sourcing/runs/1/proformas?status=bogus",
        "/sourcing/runs/1/proformas?limit=0",
        "/sourcing/runs/1/proformas?limit=101",
        "/sourcing/runs/1/proformas?after=-1",
        "/sourcing/runs/0/proformas",
        "/sourcing/runs/abc/proformas",
        f"/sourcing/runs/{2**63}/proformas",
        f"/sourcing/runs/1/candidates/{2**63}/proforma",
        "/sourcing/runs/1/candidates/0/proforma",
    ],
)
def test_bad_input_is_a_422_that_does_not_echo_it(
    run_two: tuple[TestClient, int], path: str
) -> None:
    response = run_two[0].get(path)

    assert response.status_code == 422
    assert "bogus" not in response.text


def test_every_pro_forma_route_is_get_only(migrated_engine: Engine) -> None:
    paths = create_app(MOCK, migrated_engine).openapi()["paths"]
    mine = [path for path in paths if path.endswith(("/proformas", "/proforma"))]

    assert sorted(mine) == [
        "/sourcing/runs/{run_id}/candidates/{candidate_id}/proforma",
        "/sourcing/runs/{run_id}/proformas",
    ]
    assert all(
        set(methods) == {"get"}
        for path, methods in paths.items()
        if not path.startswith("/triggers/")
    )


def test_the_router_module_cannot_write_or_spend() -> None:
    names = (
        "RentCastClient",
        "run_sourcing",
        "spend_estimates",
        "run_proformas",
        "enqueue_job",
        "enqueue",
        "sync_listings",
        "estimate_store",
        "save_estimate",
        "write_proformas",
        "store",
    )
    for name in names:
        assert not hasattr(proforma_routes, name), name


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_no_response_carries_owner_contact_raw_fields_or_the_key(
    run_two: tuple[TestClient, int], migrated_engine: Engine
) -> None:
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SENTINEL
    )
    with _client(migrated_engine, live) as client:
        run_id = run_two[1]
        listed = client.get(f"/sourcing/runs/{run_id}/proformas", params={"limit": 100})
        candidate_id = listed.json()["items"][0]["candidate_id"]
        detail = client.get(f"/sourcing/runs/{run_id}/candidates/{candidate_id}/proforma")

    for response in (listed, detail):
        assert SENTINEL.lower() not in response.text.lower()
        keys = set(_keys(response.json()))
        assert [key for key in keys if PERSONAL_DATA_KEY.search(key)] == []
        assert "raw" not in keys  # the stored listing never leaves the database


def test_the_detail_shows_the_comps_figures_but_not_their_addresses(
    run_two: tuple[TestClient, int], migrated_engine: Engine
) -> None:
    client, run_id = run_two
    detail = _detail(client, run_id, 1)
    comps = detail["result"]["arv"]["comps"]
    with migrated_engine.connect() as connection:
        stored = connection.execute(
            select(proforma.c.result).where(
                proforma.c.run_id == run_id, proforma.c.candidate_id == detail["candidate_id"]
            )
        ).scalar_one()

    assert len(comps) == 5
    assert all(set(comp) == {"price", "living_area_sqft", "psf", "used"} for comp in comps)
    assert detail["result"]["arv"]["median_psf"] == "479.4479"
    # The database keeps the addresses, for audit; the response carries none of them.
    addresses = [comp["address"] for comp in stored["arv"]["comps"]]
    assert len(addresses) == 5
    response_text = client.get(
        f"/sourcing/runs/{run_id}/candidates/{detail['candidate_id']}/proforma"
    ).text
    assert not any(address in response_text for address in addresses)

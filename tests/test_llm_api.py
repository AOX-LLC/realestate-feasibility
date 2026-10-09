"""The read-only endpoints over what the model stages stored, on both snapshot days."""

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from conftest import empty_database
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from llm_rows import ranked_candidates
from mls_data import answer_key
from sqlalchemy import Engine, text
from test_api import SENTINEL, _client, _walk_pages

from feasibility.api.app import create_app
from feasibility.api.routes import llm as llm_routes
from feasibility.config import DataMode, Settings
from feasibility.llm import ledger
from feasibility.llm.ledger import LlmCallRecord
from feasibility.llm.narrative_check import NarrativeDraft
from feasibility.llm.signals import SignalClaim, SignalExtraction
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
MOCK = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
PERSONAL_DATA_KEY = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.I)
HASH = re.compile(r"[0-9a-f]{64}")
# A draft that breaks the figure rule, with words that must never reach a response.
CANARY = "CANARYDRAFT"


_arrival: dict[str, int] = {}


def _draft(facts: Any, feedback: Any) -> NarrativeDraft:
    """A clean narrative for some candidates and a draft with a rounded figure for the others,
    chosen by the order the candidates first arrive in, so every attempt at one candidate agrees."""
    arv = facts["figures"]["arv"]
    if _arrival.setdefault(arv, len(_arrival)) % 2 == 0:
        return NarrativeDraft(
            summary=f"{CANARY} profit is about $108k.", risks=[], checks_before_offer=[]
        )
    return NarrativeDraft(
        summary=f"Profit is {facts['figures']['profit']}.",
        risks=[],
        checks_before_offer=["Confirm the survey."],
    )


def _extract(remarks: str) -> SignalExtraction:
    quote = " ".join(remarks.split())[:60]
    return SignalExtraction(
        signals=[SignalClaim(code="as_is_sale", quote=quote)], injection_suspected=False
    )


@pytest.fixture(scope="module")
def runs(migrated_engine: Engine) -> Iterator[tuple[TestClient, int, int]]:
    """The client and the ids of day one's and day two's runs. Built once: nothing here writes
    through the API, and this module's other tests never use the function-scoped `engine`."""
    empty_database(migrated_engine)
    seed(migrated_engine, MOCK)
    model = RunModel(extractions=_extract, draft=_draft, small_cost="0.004", mid_cost="0.012")
    one = run_sourcing(migrated_engine, MOCK, "dallas", DAY_ONE, model=model)
    two = run_sourcing(migrated_engine, MOCK, "dallas", DAY_TWO, model=model)
    with _client(migrated_engine) as client:
        yield client, one.run_id, two.run_id
    empty_database(migrated_engine)


def _narratives(client: TestClient, run_id: int, **params: Any) -> list[dict[str, Any]]:
    pages = _walk_pages(client, f"/sourcing/runs/{run_id}/narratives", **params)
    return [item for page in pages for item in page]


def _detail(client: TestClient, run_id: int, candidate_id: int) -> dict[str, Any]:
    response = client.get(f"/sourcing/runs/{run_id}/candidates/{candidate_id}/llm")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


# --- one candidate ------------------------------------------------------------------------------


def test_an_accepted_narrative_and_its_signals_are_served_together(
    runs: tuple[TestClient, int, int],
) -> None:
    client, one, _ = runs
    accepted = [n for n in _narratives(client, one) if n["status"] == "accepted"]
    assert accepted, "the alternating draft must accept at least one narrative"

    body = _detail(client, one, accepted[0]["candidate_id"])

    assert body["run_id"] == one
    assert body["candidate_id"] == accepted[0]["candidate_id"]
    narrative = body["narrative"]
    assert narrative["status"] == "accepted"
    assert narrative["reason"] is None
    assert narrative["summary"].startswith("Profit is $")
    assert narrative["checks_before_offer"] == ["Confirm the survey."]
    assert [f["key"] for f in narrative["figures_quoted"]] == ["profit"]
    assert narrative["check"] == {"passed": True, "attempts": 1, "violation_kinds": []}
    assert narrative["model"]["reused"] is False
    assert narrative["model"]["tier"] == "mid"
    assert "profit" in narrative["facts"]["figures"]
    signals = body["signals"]
    assert signals["status"] == "extracted"
    assert {s["code"] for s in signals["signals"]} >= {"as_is_sale"}
    quote = next(s for s in signals["signals"] if s["code"] == "as_is_sale")
    assert quote["source"] == "remarks"
    assert quote["quote"]
    assert signals["extraction"]["prompt_id"] == "signals.extract"


def test_a_rejected_narrative_is_served_as_status_reason_and_kinds_only(
    runs: tuple[TestClient, int, int],
) -> None:
    client, one, _ = runs
    rejected = [n for n in _narratives(client, one) if n["status"] == "rejected"]
    assert rejected, "the alternating draft must reject at least one narrative"

    response = client.get(f"/sourcing/runs/{one}/candidates/{rejected[0]['candidate_id']}/llm")
    narrative = response.json()["narrative"]

    assert narrative["status"] == "rejected"
    assert narrative["reason"] == "figure_check"
    assert narrative["summary"] is None
    assert narrative["risks"] == []
    assert narrative["checks_before_offer"] == []
    assert narrative["figures_quoted"] == []
    assert narrative["check"]["passed"] is False
    assert narrative["check"]["attempts"] == 2
    assert set(narrative["check"]["violation_kinds"]) >= {"unlisted_figure"}
    # Not the draft, and not the token a violation held.
    assert CANARY not in response.text
    assert "108k" not in response.text


def test_a_not_eligible_candidate_has_signals_and_a_narrative_row_with_no_text(
    runs: tuple[TestClient, int, int],
) -> None:
    client, one, _ = runs
    skipped = next(n for n in _narratives(client, one) if n["status"] == "not_eligible")

    body = _detail(client, one, skipped["candidate_id"])

    assert body["narrative"]["reason"] == "proforma_no_arv"
    assert body["narrative"]["summary"] is None
    assert body["narrative"]["check"] is None
    assert body["narrative"]["facts"] == {"figures": {}, "codes": []}
    assert body["signals"] is not None


@pytest.mark.parametrize(
    "path",
    [
        "/sourcing/runs/999999/candidates/1/llm",
        "/sourcing/runs/999999/narratives",
        "/sourcing/runs/999999/llm/cost",
    ],
)
def test_an_unknown_run_is_a_404(runs: tuple[TestClient, int, int], path: str) -> None:
    assert runs[0].get(path).status_code == 404


def test_a_candidate_the_run_does_not_hold_is_a_404(runs: tuple[TestClient, int, int]) -> None:
    client, one, _ = runs

    assert client.get(f"/sourcing/runs/{one}/candidates/999999/llm").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/sourcing/runs/0/llm/cost",
        "/sourcing/runs/bogus/narratives",
        "/sourcing/runs/99999999999999999999/candidates/1/llm",
        "/sourcing/runs/1/candidates/0/llm",
        "/sourcing/runs/1/narratives?status=bogus",
        "/sourcing/runs/1/narratives?limit=0",
        "/sourcing/runs/1/narratives?limit=101",
        "/sourcing/runs/1/narratives?after=-1",
        "/llm/spend?month=bogus",
        "/llm/spend?month=2026-13",
        "/llm/spend?month=2026-1",
    ],
)
def test_bad_input_is_a_422_that_does_not_echo_it(
    runs: tuple[TestClient, int, int], path: str
) -> None:
    response = runs[0].get(path)

    assert response.status_code == 422
    assert "bogus" not in response.text


# --- the list -----------------------------------------------------------------------------------


def test_the_narrative_list_walks_every_rank_once_in_pages_of_five(
    runs: tuple[TestClient, int, int],
) -> None:
    client, _, two = runs

    pages = _walk_pages(client, f"/sourcing/runs/{two}/narratives", limit=5)

    assert [len(page) for page in pages] == [5, 5, 5, 2]
    assert [item["rank"] for page in pages for item in page] == list(range(1, 18))
    last = client.get(f"/sourcing/runs/{two}/narratives", params={"after": 15, "limit": 5}).json()
    assert [item["rank"] for item in last["items"]] == [16, 17]
    assert last["next_after"] is None


def test_a_status_filter_returns_only_that_status_and_a_summary_only_when_accepted(
    runs: tuple[TestClient, int, int],
) -> None:
    client, _, two = runs

    everything = _narratives(client, two)
    not_eligible = _narratives(client, two, status="not_eligible")

    assert len(everything) == 17
    assert {item["status"] for item in not_eligible} == {"not_eligible"}
    assert len(not_eligible) == 11
    for item in everything:
        assert (item["summary"] is not None) == (item["status"] == "accepted")


# --- cost ---------------------------------------------------------------------------------------


def test_the_run_cost_adds_up_to_the_ledger(
    runs: tuple[TestClient, int, int], migrated_engine: Engine
) -> None:
    client, one, _ = runs

    body = client.get(f"/sourcing/runs/{one}/llm/cost").json()

    assert body["run_id"] == one
    assert body["modes"] == ["replay"]
    assert body["run_budget_usd"] == "1.00"
    # Ten signals at 0.004 and five narratives of two calls at most at 0.012.
    with migrated_engine.connect() as connection:
        sums = connection.execute(
            text(
                "SELECT count(*), sum(cost_usd), sum(input_tokens) FROM llm_call "
                "WHERE run_id = :run"
            ),
            {"run": one},
        ).one()
    assert body["total"]["calls"] == sums[0]
    assert Decimal(body["total"]["cost_usd"]) == sums[1]
    assert body["total"]["input_tokens"] == sums[2]
    assert Decimal(body["spent_usd"]) == sums[1]
    assert Decimal(body["remaining_usd"]) == Decimal("1.00") - sums[1]
    assert {line["key"] for line in body["by_stage"]} == {"signals", "narrative"}
    assert sum(line["calls"] for line in body["by_stage"]) == body["total"]["calls"]
    assert [line["key"] for line in body["by_model"]] == ["a-model"]
    assert body["total"]["reserved_unknown_usd"] == "0"


# --- what the API is not ------------------------------------------------------------------------


def test_every_model_route_is_get_only(migrated_engine: Engine) -> None:
    paths = create_app(MOCK, migrated_engine).openapi()["paths"]
    mine = [
        path for path in paths if path.endswith(("/llm", "/narratives", "/llm/cost", "/llm/spend"))
    ]

    assert sorted(mine) == [
        "/llm/spend",
        "/sourcing/runs/{run_id}/candidates/{candidate_id}/llm",
        "/sourcing/runs/{run_id}/llm/cost",
        "/sourcing/runs/{run_id}/narratives",
    ]
    assert all(set(methods) == {"get"} for methods in paths.values())


def test_the_router_module_cannot_call_a_model_write_or_spend() -> None:
    names = (
        "AgentClient",
        "MeteredClient",
        "build_model_client",
        "default_model",
        "open_client",
        "run_signals",
        "run_narratives",
        "run_sourcing",
        "aox_agent_core",
        "anthropic",
        "ledger",
        "store",
        "write_signals",
        "write_narrative",
        "cache_result",
        "record_call",
        "enqueue_job",
        "RentCastClient",
    )
    for name in names:
        assert not hasattr(llm_routes, name), name


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_no_response_carries_a_personal_key_a_planted_string_a_hash_or_the_api_key(
    runs: tuple[TestClient, int, int], migrated_engine: Engine
) -> None:
    client, one, two = runs
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SENTINEL
    )
    planted = [
        string
        for entry in answer_key().values()
        if "personal:residual" not in entry["tags"]
        for string in entry["planted_personal"]
    ]
    assert planted
    paths = [
        f"/sourcing/runs/{two}/narratives?limit=100",
        f"/sourcing/runs/{two}/llm/cost",
        f"/sourcing/runs/{one}/llm/cost",
        "/llm/spend",
    ]
    with _client(migrated_engine, live) as secured:
        for narrative in _narratives(client, two):
            paths.append(f"/sourcing/runs/{two}/candidates/{narrative['candidate_id']}/llm")
        responses = [secured.get(path) for path in paths]

    assert len(responses) > 20
    for response in responses:
        assert response.status_code == 200
        assert SENTINEL.lower() not in response.text.lower()
        assert not HASH.search(response.text), "an input hash or replay key is served"
        assert not any(string in response.text for string in planted)
        assert [key for key in set(_keys(response.json())) if PERSONAL_DATA_KEY.search(key)] == []


# --- tests that empty the database, last because the fixture above is built once ---------------


def test_a_run_without_model_rows_serves_nulls(migrated_engine: Engine) -> None:
    empty_database(migrated_engine)
    run = ranked_candidates(migrated_engine)
    candidate_id = run["candidates"][0]["candidate_id"]

    with _client(migrated_engine) as client:
        body = _detail(client, run["run_id"], candidate_id)

    assert body["signals"] is None
    assert body["narrative"] is None
    empty_database(migrated_engine)


def test_a_run_with_no_calls_costs_nothing(migrated_engine: Engine) -> None:
    empty_database(migrated_engine)
    run = ranked_candidates(migrated_engine)

    with _client(migrated_engine) as client:
        body = client.get(f"/sourcing/runs/{run['run_id']}/llm/cost").json()

    assert body["total"]["calls"] == 0
    assert body["by_stage"] == []
    assert body["modes"] == []
    assert body["remaining_usd"] == "1.00"
    empty_database(migrated_engine)


def test_the_monthly_spend_counts_billable_calls_of_that_month(migrated_engine: Engine) -> None:
    empty_database(migrated_engine)
    when = datetime(2026, 10, 9, 12, tzinfo=UTC)
    base = LlmCallRecord(
        stage="signals",
        prompt_id="signals.extract",
        prompt_version=1,
        input_sha256="a" * 64,
        tier="small",
        mode="record",
        outcome="ok",
        reserved_usd=Decimal("0.05"),
        cost_usd=Decimal("0.004"),
        called_at=when,
    )
    ledger.record_call(migrated_engine, base)
    ledger.record_call(
        migrated_engine,
        replace(base, outcome="provider_error", cost_usd=None),
    )
    ledger.record_call(migrated_engine, replace(base, mode="replay"))
    ledger.record_call(
        migrated_engine,
        replace(base, called_at=datetime(2026, 9, 30, 23, tzinfo=UTC)),
    )

    with _client(migrated_engine) as client:
        body = client.get("/llm/spend", params={"month": "2026-10"}).json()
        other = client.get("/llm/spend", params={"month": "2026-09"}).json()
        current = client.get("/llm/spend").json()

    assert body == {
        "month": "2026-10",
        "billable_calls": 2,
        "refused_calls": 0,
        "cost_usd": "0.004000",
        "reserved_unknown_usd": "0.050000",
        "spent_usd": "0.054000",
        "monthly_budget_usd": "10.00",
        "remaining_usd": "9.946000",
    }
    assert other["billable_calls"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}", current["month"])
    empty_database(migrated_engine)

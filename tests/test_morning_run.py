"""The morning run end to end, in one process: the trigger the scheduler calls, the worker, the
sourcing run and its model stages, the brief, and mock delivery to Notion and Slack.

The model is the scripted `RunModel` (it quotes remarks, drafts plain narratives); the committed
recordings are not used here, so a prompt change does not break this file. Mock delivery leaves
its requests and PDFs in MEDIA_OUT, which is where these tests read them."""

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aox_agent_core.errors import ReplayMissError
from brief_support import DAY_ONE, DAY_TWO, FREE, quoting_model, rows
from conftest import empty_database
from delivery_harness import Faults, LossyNotion, LossySlack, notion_client, settings, slack_client
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from sqlalchemy import Engine, text
from test_api import TRIGGER_HEADERS

from feasibility.api.app import create_app
from feasibility.delivery import deliver as deliver_module
from feasibility.delivery.deliver import Clients
from feasibility.jobs import queue
from feasibility.jobs.handlers import build_registry
from feasibility.jobs.payloads import BriefDeliverPayload, MorningRunPayload
from feasibility.jobs.worker import Worker
from feasibility.snapshot.load import seed


def drain(engine: Engine, config: Any) -> int:
    """Run jobs until the queue is idle (a job that queues another is run too)."""
    worker = Worker(engine, config, build_registry(), worker_id="chain")
    ran = 0
    while worker.run_once():
        ran += 1
        assert ran < 20, "the queue never went idle"
    return ran


def trigger(client: TestClient, day: str) -> Any:
    return client.post(
        "/triggers/morning", json={"market": "dallas", "as_of": day}, headers=TRIGGER_HEADERS
    )


def requests_of(folder: Path, target: str) -> list[dict[str, Any]]:
    path = folder / f"mock-{target}-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def jobs(engine: Engine) -> list[tuple[str, str]]:
    return [(j.kind, j.status) for j in rows(engine, "SELECT kind, status FROM job ORDER BY id")]


@dataclass
class Chain:
    engine: Engine
    config: Any
    client: TestClient
    model: RunModel
    media: Path


@pytest.fixture(scope="module")
def chain(migrated_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Chain]:
    empty_database(migrated_engine)
    media = tmp_path_factory.mktemp("media")
    config = settings(media_out=media)
    seed(migrated_engine, config)
    model = quoting_model()
    patch = pytest.MonkeyPatch()
    patch.setattr("feasibility.llm.run.default_model", lambda _: model)
    with TestClient(create_app(config, migrated_engine)) as client:
        yield Chain(migrated_engine, config, client, model, media)
    patch.undo()
    empty_database(migrated_engine)


# --- day one, day two, and the same day again ---------------------------------------------------
# The three steps share one database, so they run in order (the tests below read what they left).


def test_the_trigger_queues_the_chain_and_the_worker_runs_it_to_the_end(chain: Chain) -> None:
    response = trigger(chain.client, "2026-10-01")

    assert response.status_code == 202 and response.json()["already_active"] is False
    assert jobs(chain.engine) == [("morning.run", "queued")]  # the trigger ran nothing itself
    assert drain(chain.engine, chain.config) == 2

    assert jobs(chain.engine) == [("morning.run", "done"), ("brief.deliver", "done")]
    run = rows(chain.engine, "SELECT id, status, error, data_mode FROM sourcing_run")[0]
    assert (run.status, run.error, run.data_mode) == ("completed", None, "mock")


def test_day_one_delivered_five_rows_a_digest_and_five_files(chain: Chain) -> None:
    run = rows(chain.engine, "SELECT id FROM sourcing_run WHERE as_of = :d", d=DAY_ONE)[0].id
    brief = rows(chain.engine, "SELECT completeness, content FROM brief WHERE run_id = :r", r=run)[
        0
    ]
    sent = rows(
        chain.engine,
        "SELECT target, split_part(item, ':', 1) AS kind, status, mode FROM delivery "
        "WHERE run_id = :r",
        r=run,
    )

    assert brief.completeness == "complete" and len(brief.content["candidates"]) == 5
    assert Counter((r.target, r.kind, r.status, r.mode) for r in sent) == Counter(
        {
            ("notion", "row", "sent", "mock"): 5,
            ("slack", "digest", "sent", "mock"): 1,
            ("slack", "file", "sent", "mock"): 5,
        }
    )
    # The model stages ran once for the day and no value-estimate call was made against RentCast.
    assert rows(chain.engine, "SELECT count(*) AS n FROM api_budget")[0].n == 0
    assert rows(chain.engine, "SELECT count(*) AS n FROM llm_call")[0].n == len(chain.model.calls)


def test_the_mock_delivery_left_what_it_would_have_sent_in_the_media_folder(chain: Chain) -> None:
    folder = chain.media / "dallas-2026-10-01"

    assert len(list(folder.glob("pro-forma-rank-*.pdf"))) == 5
    assert all(p.read_bytes().startswith(b"%PDF") for p in folder.glob("*.pdf"))
    assert len(requests_of(folder, "notion")) == 11
    assert len(requests_of(folder, "slack")) == 16  # the digest, and three calls a file
    assert requests_of(folder, "slack")[0]["path"] == "/chat.postMessage"


def test_day_two_updates_five_rows_creates_one_and_posts_a_new_digest(chain: Chain) -> None:
    assert trigger(chain.client, "2026-10-02").status_code == 202

    drain(chain.engine, chain.config)

    run = rows(chain.engine, "SELECT id FROM sourcing_run WHERE as_of = :d", d=DAY_TWO)[0].id
    sent = rows(chain.engine, "SELECT target, status FROM delivery WHERE run_id = :r", r=run)
    assert Counter((r.target, r.status) for r in sent) == Counter(
        {("notion", "sent"): 6, ("slack", "sent"): 7}
    )
    folder = chain.media / "dallas-2026-10-02"
    notion = Counter(
        f"{r['method']} {r['path'].rsplit('/', 1)[0]}" for r in requests_of(folder, "notion")
    )
    assert notion["PATCH /v1/pages"] == 5 and notion["POST /v1"] == 1
    assert len(requests_of(folder, "notion")) == 8 and len(requests_of(folder, "slack")) == 19
    assert len(list(folder.glob("pro-forma-rank-*.pdf"))) == 6


def test_the_same_day_again_makes_no_model_call_and_no_request(chain: Chain) -> None:
    run = rows(chain.engine, "SELECT id FROM sourcing_run WHERE as_of = :d", d=DAY_TWO)[0].id
    before_hash = rows(chain.engine, "SELECT content_sha256 FROM brief WHERE run_id = :r", r=run)[0]
    calls, ledger_before = (
        len(chain.model.calls),
        rows(chain.engine, "SELECT id, status, attempts FROM delivery ORDER BY id"),
    )
    folder = chain.media / "dallas-2026-10-02"
    sent_before = {t: len(requests_of(folder, t)) for t in ("notion", "slack")}

    assert trigger(chain.client, "2026-10-02").status_code == 202
    drain(chain.engine, chain.config)

    assert len(chain.model.calls) == calls  # every result was cached
    after_hash = rows(chain.engine, "SELECT content_sha256 FROM brief WHERE run_id = :r", r=run)[0]
    assert after_hash == before_hash  # the same brief, byte for byte
    assert {t: len(requests_of(folder, t)) for t in ("notion", "slack")} == sent_before
    assert (
        rows(chain.engine, "SELECT id, status, attempts FROM delivery ORDER BY id") == ledger_before
    )
    assert [s for _, s in jobs(chain.engine)] == ["done"] * len(jobs(chain.engine))


def test_the_trigger_alone_is_not_enough_a_wrong_token_starts_nothing(chain: Chain) -> None:
    before = jobs(chain.engine)

    refused = chain.client.post(
        "/triggers/morning", json={"market": "dallas", "as_of": "2026-10-02"}
    )

    assert refused.status_code == 401
    assert jobs(chain.engine) == before


# --- what the worker does with a delivery that does not finish -----------------------------------
# Each of these builds its own day, so they follow the shared chain above.


@pytest.fixture
def day_one(migrated_engine: Engine, tmp_path: Path) -> Iterator[tuple[Engine, Any, int]]:
    from feasibility.sourcing.run import run_sourcing

    empty_database(migrated_engine)
    config = settings(media_out=tmp_path)
    seed(migrated_engine, config)
    result = run_sourcing(migrated_engine, config, "dallas", DAY_ONE, model=quoting_model())
    yield migrated_engine, config, result.run_id
    empty_database(migrated_engine)


def queue_delivery(engine: Engine, run_id: int) -> None:
    with engine.begin() as connection:
        queue.enqueue(connection, "brief.deliver", BriefDeliverPayload(run_id=run_id))


def job_state(engine: Engine) -> Any:
    return rows(
        engine, "SELECT status, attempts, last_error FROM job WHERE kind = 'brief.deliver'"
    )[0]


def test_a_slack_post_that_may_have_happened_ends_the_job_dead_and_says_why(
    day_one: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, config, run_id = day_one
    lost = LossySlack(Faults(lose_reply=["/chat.postMessage"]))
    clients = Clients(notion_client(), slack_client(lost))
    monkeypatch.setattr(deliver_module, "build_clients", lambda *args, **kwargs: clients)
    queue_delivery(engine, run_id)

    drain(engine, config)

    state = job_state(engine)
    assert state.status == "dead" and state.last_error.startswith("DeliveryUnknownOutcomeError")
    assert state.attempts == 1  # a person decides; the job is not retried


def test_a_row_that_keeps_failing_requeues_the_job_and_the_retry_sends_only_what_is_left(
    day_one: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, config, run_id = day_one
    broken = LossyNotion(Faults(statuses={"/v1/pages": [None, None, 429, 429, 429, 429]}))
    clients = Clients(notion_client(broken), slack_client())
    monkeypatch.setattr(deliver_module, "build_clients", lambda *args, **kwargs: clients)
    queue_delivery(engine, run_id)
    worker = Worker(engine, config, build_registry(), worker_id="chain")

    worker.run_once()

    first = job_state(engine)
    assert first.status == "queued" and first.last_error.startswith("DeliveryIncompleteError")
    with engine.begin() as connection:  # skip the retry delay
        connection.execute(text("UPDATE job SET run_after = now() - interval '1 minute'"))
    clients2 = Clients(notion_client(), slack_client())
    monkeypatch.setattr(deliver_module, "build_clients", lambda *args, **kwargs: clients2)

    worker.run_once()

    assert job_state(engine).status == "done"
    notion = clients2.notion
    assert notion is not None
    assert len(notion._transport.requests) == 3  # type: ignore[attr-defined]  # schema, find, create
    slack = clients2.slack
    assert slack is not None and slack._transport.requests == []  # type: ignore[attr-defined]


def test_bad_credentials_end_the_job_dead_without_retrying(
    day_one: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, config, run_id = day_one
    refused = LossyNotion(Faults(statuses={"/v1/databases/" + "0" * 32: [401]}))
    clients = Clients(notion_client(refused), slack_client())
    monkeypatch.setattr(deliver_module, "build_clients", lambda *args, **kwargs: clients)
    queue_delivery(engine, run_id)

    drain(engine, config)

    state = job_state(engine)
    assert state.status == "dead" and state.last_error.startswith("DeliveryConfigError")
    assert "http_401" in state.last_error


def test_a_run_whose_model_stage_failed_is_delivered_partial_with_a_notice(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_database(migrated_engine)
    config = settings(media_out=tmp_path)
    seed(migrated_engine, config)
    broken = RunModel(failures={1: ReplayMissError("no recording", key="k", path="p")}, **FREE)
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda _: broken)
    with migrated_engine.begin() as connection:
        queue.enqueue(
            connection,
            "morning.run",
            MorningRunPayload(market="dallas", as_of=DAY_ONE),
        )

    drain(migrated_engine, config)

    assert jobs(migrated_engine) == [("morning.run", "dead"), ("brief.deliver", "done")]
    digest = requests_of(tmp_path / "dallas-2026-10-01", "slack")[0]["body"]
    assert "Partial" in json.dumps(digest["blocks"])
    assert "Not available today." in json.dumps(digest["blocks"])
    assert len(list((tmp_path / "dallas-2026-10-01").glob("*.pdf"))) == 5
    empty_database(migrated_engine)

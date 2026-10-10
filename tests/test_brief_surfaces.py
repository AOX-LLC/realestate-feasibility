"""The brief's surfaces: the read endpoint, the command line and the jobs that build it."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from aox_agent_core.errors import ProviderUnavailableError, ReplayMissError
from brief_support import (
    DAY_ONE,
    DAY_TWO,
    FREE,
    built,
    keys_matching,
    quoting_model,
    rows,
)
from conftest import empty_database
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from sqlalchemy import Engine, text
from test_api import READ_HEADERS, _settings
from typer.testing import CliRunner

from feasibility import cli
from feasibility.api.app import create_app
from feasibility.delivery import store
from feasibility.delivery.brief import (
    FOOTER,
    NARRATIVE_LABEL,
)
from feasibility.jobs import queue
from feasibility.jobs.handlers import build_registry
from feasibility.jobs.payloads import MorningRunPayload
from feasibility.jobs.worker import Worker
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing


@pytest.fixture(scope="module")
def days(migrated_engine: Engine) -> Iterator[tuple[Engine, SourcingResult, SourcingResult]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = quoting_model()
    one = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE, model=model)
    two = run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO, model=model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    return migrated_engine


# --- the read endpoint --------------------------------------------------------------------------


def test_the_brief_endpoint_serves_the_stored_brief_behind_the_read_token(days: Any) -> None:
    engine, one, _ = days
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        url = f"/sourcing/runs/{one.run_id}/brief"
        assert client.get(url).status_code == 401
        before = client.get(url, headers=READ_HEADERS)
        with engine.begin() as connection:
            digest = store.write_brief(connection, built(engine, one.run_id))
        after = client.get(url, headers=READ_HEADERS)
        missing_run = client.get("/sourcing/runs/987654/brief", headers=READ_HEADERS)

    assert before.status_code == 404 or before.json()["content_sha256"] == digest
    assert after.status_code == 200
    body = after.json()
    assert body["content_sha256"] == digest
    assert body["brief"]["shown"] == 5
    assert not keys_matching(body)
    assert missing_run.status_code == 404


# --- the command line ---------------------------------------------------------------------------


@pytest.fixture
def cli_engine(days: Any, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = days[0]
    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    monkeypatch.setattr(cli, "get_settings", _settings)
    return engine


def test_brief_build_then_show_prints_the_candidates(cli_engine: Engine, days: Any) -> None:
    runner = CliRunner()

    built_result = runner.invoke(cli.app, ["brief", "build", "--run-id", str(days[2].run_id)])
    shown = runner.invoke(cli.app, ["brief", "show", "--run-id", str(days[2].run_id)])

    assert built_result.exit_code == 0, built_result.output
    assert "brief built, complete, 6 candidates" in built_result.output
    assert shown.exit_code == 0, shown.output
    assert "6 shown of 17 ranked" in shown.output
    assert FOOTER in shown.output
    assert NARRATIVE_LABEL in shown.output


def test_brief_show_without_a_brief_and_for_a_missing_run_exit_two(
    cli_engine: Engine, days: Any
) -> None:
    runner = CliRunner()
    with cli_engine.begin() as connection:
        connection.execute(text("DELETE FROM brief"))

    nothing = runner.invoke(cli.app, ["brief", "show", "--run-id", str(days[1].run_id)])
    no_run = runner.invoke(cli.app, ["brief", "show", "--run-id", "987654"])
    not_ready = runner.invoke(cli.app, ["brief", "build", "--run-id", "987654"])

    assert nothing.exit_code == 2
    assert no_run.exit_code == 2
    assert not_ready.exit_code == 2


# --- the jobs -----------------------------------------------------------------------------------


def _queue_morning(engine: Engine, day: date) -> None:
    with engine.begin() as connection:
        queue.enqueue(connection, "morning.run", MorningRunPayload(market="dallas", as_of=day))


def _job_rows(engine: Engine) -> list[Any]:
    return rows(engine, "SELECT kind, status, payload, dedupe_key, last_error FROM job ORDER BY id")


def test_a_morning_run_queues_the_brief_of_its_run(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda settings: RunModel(**FREE))
    _queue_morning(seeded, DAY_ONE)

    assert Worker(seeded, _settings(), build_registry(), worker_id="w").run_once()

    run_id = rows(seeded, "SELECT id FROM sourcing_run")[0].id
    queued = _job_rows(seeded)
    assert [(j.kind, j.status) for j in queued] == [
        ("morning.run", "done"),
        ("brief.deliver", "queued"),
    ]
    assert queued[1].payload == {"run_id": run_id}
    assert queued[1].dedupe_key == f"brief.deliver:{run_id}"


def test_the_brief_job_builds_and_stores_the_brief(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda settings: RunModel(**FREE))
    _queue_morning(seeded, DAY_ONE)
    worker = Worker(seeded, _settings(), build_registry(), worker_id="w")
    worker.run_once()

    assert worker.run_once()

    assert [(j.kind, j.status) for j in _job_rows(seeded)] == [
        ("morning.run", "done"),
        ("brief.deliver", "done"),
    ]
    stored = rows(seeded, "SELECT completeness, content->>'shown' AS shown FROM brief")
    assert [(r.completeness, r.shown) for r in stored] == [("complete", "5")]


def test_a_missing_recording_still_queues_a_partial_brief_and_the_job_goes_dead(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = RunModel(failures={1: ReplayMissError("no recording", key="k", path="p")}, **FREE)
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda settings: broken)
    _queue_morning(seeded, DAY_ONE)
    worker = Worker(seeded, _settings(), build_registry(), worker_id="w")

    worker.run_once()
    worker.run_once()

    jobs = _job_rows(seeded)
    assert [(j.kind, j.status) for j in jobs] == [
        ("morning.run", "dead"),
        ("brief.deliver", "done"),
    ]
    assert jobs[0].last_error.startswith("PermanentModelError")
    stored = rows(seeded, "SELECT completeness, content->>'notice' AS notice FROM brief")
    assert [(r.completeness, r.notice) for r in stored] == [("partial", "later_stage_failed")]


def test_a_provider_outage_queues_no_brief_and_the_job_is_retried(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = RunModel(failures={1: ProviderUnavailableError("503")}, **FREE)
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda settings: down)
    _queue_morning(seeded, DAY_ONE)

    Worker(seeded, _settings(), build_registry(), worker_id="w").run_once()

    jobs = _job_rows(seeded)
    assert [(j.kind, j.status) for j in jobs] == [("morning.run", "queued")]
    assert jobs[0].last_error.startswith("RetryableModelError")


def test_a_brief_for_a_missing_run_goes_straight_to_dead(seeded: Engine) -> None:
    with seeded.begin() as connection:
        queue.enqueue(
            connection,
            "brief.deliver",
            __import__("feasibility.jobs.payloads", fromlist=["x"]).BriefDeliverPayload(
                run_id=987654
            ),
        )

    Worker(seeded, _settings(), build_registry(), worker_id="w").run_once()

    assert [(j.kind, j.status) for j in _job_rows(seeded)] == [("brief.deliver", "dead")]


def test_the_partial_brief_uses_the_date_the_job_carries_and_does_not_resolve_it_again(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = RunModel(failures={1: ReplayMissError("no recording", key="k", path="p")}, **FREE)
    monkeypatch.setattr("feasibility.llm.run.default_model", lambda settings: broken)

    def must_not_resolve(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the date was resolved a second time")

    monkeypatch.setattr("feasibility.jobs.handlers.resolve_run_date", must_not_resolve)
    _queue_morning(seeded, DAY_ONE)
    worker = Worker(seeded, _settings(), build_registry(), worker_id="w")

    worker.run_once()

    assert [(j.kind) for j in _job_rows(seeded)] == ["morning.run", "brief.deliver"]

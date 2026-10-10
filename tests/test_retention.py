"""Retention beyond the attack tests: what the dry run reports, the job, the trigger, the command
and the run lock."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from retention_support import (
    AS_OF,
    add_job,
    add_llm_call,
    add_llm_result,
    add_run,
    at,
    count,
    days_ago,
    months_before,
    run_sql,
)
from sqlalchemy import Engine
from test_api import READ_HEADERS, TRIGGER_HEADERS, _settings
from typer.testing import CliRunner

from feasibility import cli
from feasibility.api.app import create_app
from feasibility.config import DataMode, Settings
from feasibility.jobs import queue
from feasibility.jobs.handlers import (
    PERMANENT_ERRORS,
    JobContext,
    build_registry,
    run_retention_prune,
)
from feasibility.jobs.payloads import RetentionPrunePayload
from feasibility.jobs.worker import Worker
from feasibility.retention.prune import RULES, RetentionPolicy, policy_from, prune
from feasibility.sourcing import store

POLICY = RetentionPolicy(
    run_days=90,
    estimate_days=90,
    model_cache_days=30,
    ledger_months=13,
    job_days=30,
    listing_days=30,
)


def populate(engine: Engine, *, spend: bool = True) -> None:
    add_run(engine, age=1, tag="newest")
    add_run(engine, age=40, tag="mid")
    add_run(engine, age=100, tag="old")
    add_run(engine, age=130, tag="older")
    call = add_llm_call(engine, called=months_before(AS_OF, 14)) if spend else None
    add_llm_result(engine, created=days_ago(45), key="a", call_id=call)
    add_job(engine, finished=days_ago(60))
    run_sql(
        engine,
        "INSERT INTO api_cache (provider, request_key, endpoint, params, body, fetched_at, "
        "expires_at) VALUES ('rentcast', 'k', '/x', CAST('{}' AS jsonb), CAST('{}' AS jsonb), "
        ":t, :t)",
        t=at(days_ago(120)),
    )


# --- the dry run and the real prune say the same ------------------------------------------------


def test_a_dry_run_reports_exactly_what_the_real_prune_then_deletes(engine: Engine) -> None:
    populate(engine)

    dry = prune(engine, POLICY, as_of=AS_OF, dry_run=True)
    real = prune(engine, POLICY, as_of=AS_OF)

    assert dry.counts == real.counts
    assert dry.dry_run is True and real.dry_run is False
    # Two old runs, and the listings and candidates that only those two were holding on to.
    assert real.counts["runs"] == 2
    assert real.counts["listing"] == 2 and real.counts["candidate"] == 2
    assert set(real.counts) == set(RULES)
    assert real.counts["llm_result"] == 1 and real.counts["llm_call"] == 1
    assert real.counts["job"] == 1 and real.counts["api_cache"] == 1


def test_each_rule_deletes_in_more_than_one_batch(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from feasibility.retention import prune as prune_module

    add_run(engine, age=1, tag="newest")
    for number in range(7):
        add_llm_call(engine, called=months_before(AS_OF, 14 + number))
    monkeypatch.setattr(prune_module, "BATCH", 3)

    report = prune(engine, POLICY, as_of=AS_OF)

    assert report.counts["llm_call"] == 7 and count(engine, "llm_call") == 0


def test_a_policy_with_longer_windows_deletes_less(engine: Engine) -> None:
    populate(engine)

    report = prune(
        engine,
        RetentionPolicy(
            run_days=110,
            estimate_days=110,
            model_cache_days=60,
            ledger_months=24,
            job_days=90,
            listing_days=30,
        ),
        as_of=AS_OF,
    )

    assert report.counts["runs"] == 1  # only the 130-day-old run
    assert report.counts["llm_call"] == 0 and report.counts["llm_result"] == 0
    assert report.counts["job"] == 0


def test_the_policy_comes_from_the_settings() -> None:
    settings = Settings(_env_file=None, retention_run_days=120, retention_ledger_months=24)  # type: ignore[call-arg]

    policy = policy_from(settings)

    assert (policy.run_days, policy.estimate_days, policy.ledger_months) == (120, 90, 24)


# --- the market's run lock ----------------------------------------------------------------------


def test_pruning_a_run_waits_for_the_markets_run_lock(engine: Engine) -> None:
    add_run(engine, age=1, tag="newest")
    old = add_run(engine, age=100, tag="old")
    finished = threading.Event()

    def pruner() -> None:
        prune(engine, POLICY, as_of=AS_OF)
        finished.set()

    with engine.begin() as holder:
        store.lock_market_runs(holder, "dallas")  # a run is being built
        thread = threading.Thread(target=pruner)
        thread.start()
        assert not finished.wait(1.5)  # it is waiting, having deleted nothing of that run
        assert count(engine, "run_candidate", f"run_id = {old.run_id}") == 1
    thread.join(30)

    assert finished.is_set()
    assert count(engine, "run_candidate", f"run_id = {old.run_id}") == 0


def test_a_run_another_prune_got_to_first_is_left_alone_and_not_counted_twice(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from feasibility.retention import prune as prune_module

    add_run(engine, age=1, tag="newest")
    old = add_run(engine, age=100, tag="old")
    real = prune_module._prune_one_run

    def another_prune_first(engine_: Engine, run_id: int, market: str, cutoff: Any) -> Any:
        real(engine_, run_id, market, cutoff)  # the other prune does the work...
        return real(engine_, run_id, market, cutoff)  # ...and ours finds nothing left to do

    monkeypatch.setattr(prune_module, "_prune_one_run", another_prune_first)

    report = prune(engine, POLICY, as_of=AS_OF)

    assert report.counts["runs"] == 0
    assert count(engine, "run_candidate", f"run_id = {old.run_id}") == 0


# --- the job, the trigger and the command -------------------------------------------------------


def test_the_job_prunes_with_the_settings_windows_and_a_dry_run_job_deletes_nothing(
    engine: Engine,
) -> None:
    populate(engine, spend=False)  # a database with no real spend, so a chosen day is allowed
    settings = _settings()
    context = JobContext(engine, settings, 1)
    before = count(engine, "run_candidate")

    run_retention_prune(RetentionPrunePayload(dry_run=True, as_of=AS_OF), context)
    assert count(engine, "run_candidate") == before
    run_retention_prune(RetentionPrunePayload(as_of=AS_OF), context)

    assert count(engine, "run_candidate") == before - 2


def test_the_trigger_queues_one_prune_of_each_kind_and_a_repeat_of_a_kind_queues_none(
    engine: Engine,
) -> None:
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        first = client.post("/triggers/retention", headers=TRIGGER_HEADERS)
        dry = client.post("/triggers/retention", headers=TRIGGER_HEADERS, json={"dry_run": True})
        again = client.post("/triggers/retention", headers=TRIGGER_HEADERS)
        dry_again = client.post(
            "/triggers/retention", headers=TRIGGER_HEADERS, json={"dry_run": True}
        )
        read_token = client.post("/triggers/retention", headers=READ_HEADERS)
        no_token = client.post("/triggers/retention")
        bad_body = client.post(
            "/triggers/retention", headers=TRIGGER_HEADERS, json={"as_of": "2020-01-01"}
        )

    # A queued dry run does not stop the weekly real prune from being queued, and the reverse.
    assert (first.status_code, first.json()["already_active"]) == (202, False)
    assert (dry.status_code, dry.json()["already_active"]) == (202, False)
    assert (again.status_code, again.json()) == (200, {"job_id": None, "already_active": True})
    assert dry_again.status_code == 200 and dry_again.json()["already_active"] is True
    assert (read_token.status_code, no_token.status_code, bad_body.status_code) == (403, 401, 422)
    jobs = run_sql(engine, "SELECT kind, payload FROM job ORDER BY id").all()
    assert [(j.kind, j.payload["dry_run"]) for j in jobs] == [
        ("retention.prune", False),
        ("retention.prune", True),
    ]


def test_the_worker_runs_the_prune_job_to_done(engine: Engine) -> None:
    add_run(engine, age=1, tag="newest")
    with engine.begin() as connection:
        queue.enqueue(connection, "retention.prune", RetentionPrunePayload(as_of=AS_OF))

    assert Worker(engine, _settings(), build_registry(), worker_id="w").run_once()

    assert run_sql(engine, "SELECT status FROM job").one().status == "done"


@pytest.fixture
def cli_engine_(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    return engine


def test_the_command_prints_a_row_per_rule_and_a_dry_run_says_nothing_was_deleted(
    cli_engine_: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(cli_engine_, spend=False)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    before = count(cli_engine_, "run_candidate")

    dry = CliRunner().invoke(cli.app, ["retention", "prune", "--dry-run", "--as-of", "2026-12-15"])
    real = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "2026-12-15"])

    assert dry.exit_code == 0, dry.output
    assert "would delete" in dry.output and "dry run: nothing deleted" in dry.output
    for rule in RULES:
        assert rule in dry.output
    assert real.exit_code == 0 and "rows deleted" in real.output
    assert count(cli_engine_, "run_candidate") == before - 2


def test_the_command_refuses_as_of_without_dry_run_when_the_data_is_live(
    cli_engine_: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Settings(_env_file=None, data_mode=DataMode.LIVE, rentcast_api_key="x" * 20)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "get_settings", lambda: live)
    populate(cli_engine_)
    before = count(cli_engine_, "run_candidate")

    refused = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "2026-12-15"])
    dry = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "2026-12-15", "--dry-run"])
    bad = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "next week"])

    assert refused.exit_code == 2 and count(cli_engine_, "run_candidate") == before
    assert dry.exit_code == 0 and bad.exit_code == 2


def test_a_prune_from_a_chosen_day_is_refused_by_the_command_where_there_is_real_spend(
    cli_engine_: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    for months in (0, 1, 2):
        add_llm_call(cli_engine_, called=months_before(AS_OF, months))
    run_sql(cli_engine_, "UPDATE llm_call SET billable = true, mode = 'live'")

    refused = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "2030-01-15"])
    dry = CliRunner().invoke(cli.app, ["retention", "prune", "--as-of", "2030-01-15", "--dry-run"])

    assert refused.exit_code == 2 and "refused" in refused.output
    assert dry.exit_code == 0 and "llm_call" in dry.output
    assert count(cli_engine_, "llm_call") == 3  # a chosen future day would have deleted them all


# --- a pruned run cannot be briefed or delivered -------------------------------------------------


def test_a_pruned_run_cannot_be_built_into_a_brief_or_delivered(engine: Engine) -> None:
    from delivery_harness import deliver

    from feasibility.delivery.build import RunPrunedError, build_brief_snapshot
    from feasibility.delivery.notion import MockNotionTransport
    from feasibility.delivery.slack import MockSlackTransport

    add_run(engine, age=1, tag="newest")
    old = add_run(engine, age=100, tag="old")
    prune(engine, POLICY, as_of=AS_OF)
    slack, notion = MockSlackTransport(), MockNotionTransport()

    with pytest.raises(RunPrunedError):
        build_brief_snapshot(engine, old.run_id)
    with pytest.raises(RunPrunedError):
        deliver(engine, old.run_id, notion, slack)

    assert slack.requests == [] and notion.requests == []
    assert count(engine, "brief", f"run_id = {old.run_id}") == 0
    assert issubclass(RunPrunedError, PERMANENT_ERRORS)


# --- a prune from a chosen day, queued ------------------------------------------------------------


def test_a_queued_prune_from_a_chosen_day_is_refused_where_spend_is_real(engine: Engine) -> None:
    from feasibility.retention.prune import RetentionRefusedError

    add_run(engine, age=1, tag="newest")
    add_llm_call(engine, called=AS_OF)
    run_sql(engine, "UPDATE llm_call SET billable = true, mode = 'live'")
    context = JobContext(engine, _settings(), 1)
    far = AS_OF + timedelta(days=900)

    with pytest.raises(RetentionRefusedError):
        run_retention_prune(RetentionPrunePayload(as_of=far), context)
    run_retention_prune(RetentionPrunePayload(as_of=far, dry_run=True), context)

    assert count(engine, "llm_call") == 1  # the month's spend is still there
    assert issubclass(RetentionRefusedError, PERMANENT_ERRORS)


def test_a_database_that_ever_held_live_runs_refuses_it_even_in_mock_mode(engine: Engine) -> None:
    from feasibility.retention.prune import RetentionRefusedError, check_as_of_allowed

    add_run(engine, age=1, tag="newest")
    check_as_of_allowed(engine, _settings(), AS_OF, False)  # synthetic data only: allowed
    run_sql(engine, "UPDATE sourcing_run SET data_mode = 'live'")

    with pytest.raises(RetentionRefusedError):
        check_as_of_allowed(engine, _settings(), AS_OF, False)
    check_as_of_allowed(engine, _settings(), AS_OF, True)  # a dry run deletes nothing
    check_as_of_allowed(engine, _settings(), None, False)  # today's date is the normal case

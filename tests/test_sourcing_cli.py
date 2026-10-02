import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.jobs.handlers import (
    PERMANENT_ERRORS,
    JobContext,
    SourcingRunPayload,
    build_registry,
    run_sourcing_job,
)
from feasibility.jobs.worker import Worker
from feasibility.snapshot.load import seed
from feasibility.sourcing.errors import NoSnapshotForDateError, SourcingError
from feasibility.tables import job, sourcing_run

runner = CliRunner()


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def keep_root_logging() -> Iterator[None]:
    """The CLI callback replaces the root handlers with one writing to the runner's stream,
    which is closed after the call."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


@pytest.fixture
def seeded(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    seed(engine, _settings())
    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    monkeypatch.setattr(cli, "get_settings", _settings)
    return engine


def _invoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["source", *args])


def test_the_job_kind_is_registered_with_a_strict_payload() -> None:
    kind = build_registry()["sourcing.run"]
    assert kind.payload_model is SourcingRunPayload
    assert kind.handler is run_sourcing_job
    assert SourcingRunPayload(market="dallas").as_of is None
    with pytest.raises(ValidationError):
        SourcingRunPayload.model_validate({"market": "dallas", "surprise": 1})
    with pytest.raises(ValidationError):
        SourcingRunPayload.model_validate({"market": "dallas", "as_of": "not-a-date"})


def test_sourcing_errors_are_permanent() -> None:
    assert SourcingError in PERMANENT_ERRORS


def test_a_sourcing_error_sends_the_job_straight_to_dead(seeded: Engine) -> None:
    with seeded.begin() as connection:
        connection.execute(
            job.insert().values(
                kind="sourcing.run", payload={"market": "dallas", "as_of": "2026-10-09"}
            )
        )

    assert Worker(seeded, _settings(), build_registry(), worker_id="w").run_once()

    with seeded.connect() as connection:
        row = connection.execute(select(job.c.status, job.c.attempts, job.c.last_error)).one()
    assert (row.status, row.attempts) == ("dead", 1)
    assert row.last_error.startswith("NoSnapshotForDateError")


def test_the_handler_runs_the_day(seeded: Engine) -> None:
    context = JobContext(engine=seeded, settings=_settings(), job_id=1)
    run_sourcing_job(SourcingRunPayload(market="dallas", as_of=date(2026, 10, 1)), context)
    with seeded.connect() as connection:
        runs = connection.execute(select(sourcing_run.c.as_of, sourcing_run.c.status)).all()
    assert [tuple(run) for run in runs] == [(date(2026, 10, 1), "completed")]


def test_run_prints_the_counts_and_the_top_ten(seeded: Engine) -> None:
    result = _invoke("run", "--as-of", "2026-10-01")

    assert result.exit_code == 0, result.output
    assert "ranked 12" in result.output
    assert "listing_filtered 6" in result.output
    lines = result.output.splitlines()
    top = lines[lines.index("top 10 ranked:") + 1 :]
    assert len(top) == 10
    assert top[0].startswith("1 89.39 378000.00 ")


def test_run_in_mock_mode_needs_a_date_and_lists_the_available_ones(seeded: Engine) -> None:
    result = _invoke("run")
    assert result.exit_code == 2
    assert "2026-10-01, 2026-10-02" in result.output


def test_an_out_of_order_date_exits_two(seeded: Engine) -> None:
    assert _invoke("run", "--as-of", "2026-10-02").exit_code == 0
    result = _invoke("run", "--as-of", "2026-10-01")
    assert result.exit_code == 2
    assert "already complete" in result.output


def test_enqueue_twice_yields_one_queued_job(seeded: Engine) -> None:
    first = _invoke("run", "--as-of", "2026-10-01", "--enqueue")
    second = _invoke("run", "--as-of", "2026-10-01", "--enqueue")

    assert first.output.startswith("queued job")
    assert "already active" in second.output
    with seeded.connect() as connection:
        rows = connection.execute(select(job.c.kind, job.c.dedupe_key, job.c.status)).all()
    assert [tuple(row) for row in rows] == [
        ("sourcing.run", "sourcing.run:dallas:2026-10-01", "queued")
    ]


def test_enqueue_refuses_a_date_the_snapshot_does_not_hold(seeded: Engine) -> None:
    result = _invoke("run", "--as-of", "2026-10-09", "--enqueue")
    assert result.exit_code == 2
    with seeded.connect() as connection:
        assert connection.execute(select(job.c.id)).all() == []


def test_show_lists_a_runs_candidates_by_status(seeded: Engine) -> None:
    _invoke("run", "--as-of", "2026-10-01")

    ranked = _invoke("show", "--limit", "3")
    assert ranked.exit_code == 0
    assert ranked.output.splitlines()[0].endswith(", ranked: 3 shown")

    filtered = _invoke("show", "--status", "filtered")
    assert "year_built,land_to_total" in filtered.output
    assert len(filtered.output.splitlines()) == 3

    assert _invoke("show", "--status", "bogus").exit_code != 0


def test_show_without_a_run_says_so(seeded: Engine) -> None:
    result = _invoke("show")
    assert result.exit_code == 1
    assert "no completed run" in result.output


def test_the_date_error_is_a_sourcing_error() -> None:
    assert issubclass(NoSnapshotForDateError, SourcingError)

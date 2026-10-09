"""`feasibility llm show` and `feasibility llm cost`: what they print, and that they only read."""

import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from conftest import empty_database
from llm_fakes import RunModel
from llm_rows import ranked_candidates
from sqlalchemy import Engine, func, select, text
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.llm.narrative_check import NarrativeDraft
from feasibility.llm.signals import SignalClaim, SignalExtraction
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import (
    candidate_narrative,
    candidate_signals,
    llm_call,
    llm_result,
    sourcing_run,
)

DAY_ONE = date(2026, 10, 1)
CANARY = "CANARYDRAFT"
runner = CliRunner()


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


_arrival: dict[str, int] = {}


def _draft(facts: Any, feedback: Any) -> NarrativeDraft:
    """Rejects some candidates and passes the others, by the order they first arrive in."""
    if _arrival.setdefault(facts["figures"]["arv"], len(_arrival)) % 2 == 0:
        return NarrativeDraft(summary=f"{CANARY} about $108k.", risks=[], checks_before_offer=[])
    return NarrativeDraft(summary="A plain summary.", risks=[], checks_before_offer=[])


def _extract(remarks: str) -> SignalExtraction:
    return SignalExtraction(
        signals=[SignalClaim(code="as_is_sale", quote=" ".join(remarks.split())[:60])],
        injection_suspected=False,
    )


@pytest.fixture(autouse=True)
def keep_root_logging() -> Iterator[None]:
    """The CLI callback replaces the root handlers with one writing to the runner's stream,
    which is closed after the call."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


@pytest.fixture(scope="module")
def day_one(migrated_engine: Engine) -> Iterator[tuple[Engine, int]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = RunModel(extractions=_extract, draft=_draft, small_cost="0.004", mid_cost="0.012")
    result = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE, model=model)
    patch = pytest.MonkeyPatch()
    patch.setattr(cli, "get_engine", lambda: migrated_engine)
    patch.setattr(cli, "get_settings", _settings)
    yield migrated_engine, result.run_id
    patch.undo()
    empty_database(migrated_engine)


def _invoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["llm", *args])


def _candidate(engine: Engine, run_id: int, narrative_status: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT candidate_id FROM candidate_narrative "
                    "WHERE run_id = :run AND status = :status ORDER BY candidate_id LIMIT 1"
                ),
                {"run": run_id, "status": narrative_status},
            ).scalar_one()
        )


def test_show_prints_the_signals_and_an_accepted_narrative(day_one: tuple[Engine, int]) -> None:
    engine, run_id = day_one

    result = _invoke("show", str(_candidate(engine, run_id, "accepted")), "--run-id", str(run_id))

    assert result.exit_code == 0, result.output
    assert f"run {run_id}, candidate" in result.output
    assert "signals: extracted" in result.output
    assert "as_is_sale" in result.output
    assert "narrative: accepted" in result.output
    assert "A plain summary." in result.output
    assert "check: passed after 1 attempt(s)" in result.output


def test_show_prints_a_rejected_narrative_without_its_text(day_one: tuple[Engine, int]) -> None:
    engine, run_id = day_one

    result = _invoke("show", str(_candidate(engine, run_id, "rejected")))

    assert result.exit_code == 0, result.output
    assert "narrative: rejected (figure_check)" in result.output
    assert "check: failed after 2 attempt(s); broke: unlisted_figure" in result.output
    assert CANARY not in result.output
    assert "108k" not in result.output


def test_show_defaults_to_the_latest_completed_run(day_one: tuple[Engine, int]) -> None:
    engine, run_id = day_one

    result = _invoke("show", str(_candidate(engine, run_id, "not_eligible")))

    assert result.exit_code == 0
    assert result.output.startswith(f"run {run_id}, candidate")
    assert "narrative: not_eligible (proforma_no_arv)" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["show", "1", "--run-id", "999999"],
        ["show", "999999"],
        ["cost", "--run-id", "999999"],
    ],
)
def test_a_missing_run_or_candidate_exits_two(day_one: tuple[Engine, int], args: list[str]) -> None:
    result = _invoke(*args)

    assert result.exit_code == 2
    assert "no run" in result.output or "not in run" in result.output


def test_cost_prints_the_run_by_stage_and_by_model(day_one: tuple[Engine, int]) -> None:
    _, run_id = day_one

    result = _invoke("cost", "--run-id", str(run_id))

    assert result.exit_code == 0, result.output
    assert f"run {run_id} model calls: modes replay" in result.output
    assert "by stage:" in result.output
    assert "signals" in result.output
    assert "narrative" in result.output
    assert "by model:" in result.output
    assert "a-model" in result.output
    assert "of a $1.00 run budget" in result.output


def test_cost_with_a_month_prints_the_months_billable_spend(day_one: tuple[Engine, int]) -> None:
    result = _invoke("cost", "--month", "2026-10")

    assert result.exit_code == 0, result.output
    assert "billable calls in 2026-10 (UTC): 0 sent, 0 refused" in result.output
    assert "of a $10.00 monthly budget" in result.output
    assert "model calls" not in result.output


def test_a_bad_month_exits_two(day_one: tuple[Engine, int]) -> None:
    assert _invoke("cost", "--month", "bogus").exit_code == 2


def test_the_commands_only_read(day_one: tuple[Engine, int]) -> None:
    engine, run_id = day_one
    tables = (llm_call, llm_result, candidate_signals, candidate_narrative, sourcing_run)

    def counts() -> list[int]:
        with engine.connect() as connection:
            return [
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in tables
            ]

    before = counts()
    _invoke("show", str(_candidate(engine, run_id, "accepted")))
    _invoke("cost")
    _invoke("cost", "--month", "2026-10")

    assert counts() == before


# --- tests that empty the database, last because the fixture above is built once ---------------


def test_with_no_completed_run_both_commands_exit_two(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_database(migrated_engine)
    monkeypatch.setattr(cli, "get_engine", lambda: migrated_engine)
    monkeypatch.setattr(cli, "get_settings", _settings)

    assert _invoke("show", "1").exit_code == 2
    assert _invoke("cost").exit_code == 2


def test_a_run_with_no_stored_rows_says_so(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_database(migrated_engine)
    run = ranked_candidates(migrated_engine)
    monkeypatch.setattr(cli, "get_engine", lambda: migrated_engine)
    monkeypatch.setattr(cli, "get_settings", _settings)

    result = _invoke(
        "show", str(run["candidates"][0]["candidate_id"]), "--run-id", str(run["run_id"])
    )

    assert result.exit_code == 0
    assert "signals: none stored" in result.output
    assert "narrative: none stored" in result.output
    empty_database(migrated_engine)

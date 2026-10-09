"""`feasibility proforma list` and `show` over the seeded database."""

import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from conftest import empty_database
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.proforma import store
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import run_candidate

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
SECTIONS = [
    "version",
    "status",
    "reason",
    "flags",
    "assumptions",
    "site",
    "sizing",
    "arv",
    "costs",
    "financing",
    "holding",
    "selling",
    "totals",
    "max_offer",
    "sensitivity",
]

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


@pytest.fixture(scope="module")
def ran(migrated_engine: Engine) -> dict[str, Any]:
    """Both days sourced once; the ids the tests ask for by candidate."""
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    one = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE)
    two = run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO)
    with migrated_engine.connect() as connection:
        listed = store.read_proformas(connection, two.run_id, limit=100)
        filtered = connection.execute(
            select(run_candidate.c.candidate_id)
            .where(run_candidate.c.run_id == two.run_id, run_candidate.c.status != "ranked")
            .limit(1)
        ).scalar_one()
    return {
        "engine": migrated_engine,
        "day_one": one.run_id,
        "day_two": two.run_id,
        "computed": next(i.candidate_id for i in listed if i.rank == 2),
        "no_arv": next(i.candidate_id for i in listed if i.rank == 7),
        "filtered": filtered,
    }


@pytest.fixture
def seeded(ran: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(cli, "get_engine", lambda: ran["engine"])
    monkeypatch.setattr(cli, "get_settings", _settings)
    return ran


def _invoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["proforma", *args])


def _table(output: str) -> list[list[str]]:
    """The tab-separated rows after the header line, without the leading summary."""
    lines = [line for line in output.splitlines() if "\t" in line]
    return [line.split("\t") for line in lines[1:]]


def test_list_shows_the_latest_run_in_rank_order(seeded: dict[str, Any]) -> None:
    result = _invoke("list")

    assert result.exit_code == 0
    assert result.output.splitlines()[0] == f"run {seeded['day_two']}, all statuses: 17 shown"
    rows = _table(result.output)
    assert [row[0] for row in rows] == [str(rank) for rank in range(1, 18)]
    assert rows[0][1:] == [
        "8773 ORRINMOOR TRL",
        "computed",
        "1,678,067.65",
        "1,348,504.58",
        "329,563.07",
        "19.6%",
        "448,602.06",
    ]
    assert rows[6][2:] == ["no_arv: no_estimate_yet", "none", "none", "none", "none", "none"]


def test_list_filters_by_status_and_limits(seeded: dict[str, Any]) -> None:
    computed = _table(_invoke("list", "--status", "computed").output)
    first_three = _table(_invoke("list", "--limit", "3").output)

    assert [row[0] for row in computed] == ["1", "2", "3", "4", "5", "6"]
    assert [row[0] for row in first_three] == ["1", "2", "3"]
    assert _table(_invoke("list", "--status", "unsizable").output) == []


def test_list_can_read_an_earlier_run(seeded: dict[str, Any]) -> None:
    result = _invoke("list", "--run-id", str(seeded["day_one"]), "--limit", "100")

    assert result.output.splitlines()[0] == f"run {seeded['day_one']}, all statuses: 12 shown"
    assert len(_table(result.output)) == 12


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["list", "--run-id", "999999"], "no run 999999"),
        (["show", "1", "--run-id", "999999"], "no run 999999"),
        (["show", "999999"], "candidate 999999 has no pro-forma in run"),
    ],
)
def test_a_missing_run_or_candidate_is_exit_2_with_a_message(
    seeded: dict[str, Any], args: list[str], message: str
) -> None:
    result = _invoke(*args)

    assert result.exit_code == 2
    assert message in result.output
    assert "Traceback" not in result.output


def test_a_candidate_the_run_did_not_rank_has_no_pro_forma(seeded: dict[str, Any]) -> None:
    result = _invoke("show", str(seeded["filtered"]))

    assert result.exit_code == 2
    assert "has no pro-forma in run" in result.output


def test_a_bad_status_is_refused(seeded: dict[str, Any]) -> None:
    result = _invoke("list", "--status", "bogus")

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_with_no_completed_run_there_is_nothing_to_show(
    seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sourcing_store, "latest_run_id", lambda connection, market: None)

    for args in (["list"], ["show", "1"]):
        result = _invoke(*args)
        assert result.exit_code == 2
        assert "no completed run yet" in result.output


def test_show_prints_the_sections_in_the_order_of_the_stored_result(
    seeded: dict[str, Any],
) -> None:
    result = _invoke("show", str(seeded["computed"]))

    assert result.exit_code == 0
    top_level = [
        line.split(":")[0]
        for line in result.output.splitlines()
        if line and not line.startswith((" ", "run ", "candidate", "offer price"))
    ]
    assert top_level == SECTIONS
    assert "candidate " + str(seeded["computed"]) + ", rank 2: 1893 THISTLEWANE DR" in result.output
    assert "offer price: 321,000.00" in result.output
    assert "total_cost: 1,292,480.17" in result.output  # money with separators and cents
    assert "margin: 19.4%" in result.output  # a ratio as a percent
    assert "status: illustrative" in result.output  # the assumptions say so
    assert "selling: none" not in result.output


def test_show_without_the_flag_does_not_print_the_grid(seeded: dict[str, Any]) -> None:
    result = _invoke("show", str(seeded["computed"]))

    assert "60 cells; --sensitivity prints them" in result.output
    assert "hold 6 months" not in result.output


def test_show_sensitivity_prints_three_blocks_of_five_by_four(seeded: dict[str, Any]) -> None:
    result = _invoke("show", str(seeded["computed"]), "--sensitivity")

    lines = result.output.splitlines()
    starts = [i for i, line in enumerate(lines) if "months (profit, margin" in line]
    assert [lines[i].split()[1:3] for i in starts] == [
        ["6", "months"],
        ["9", "months"],
        ["12", "months"],
    ]
    for start in starts:
        header, *rows = lines[start + 1 : start + 7]
        assert header.split("\t")[1:] == ["-10", "0", "10", "20"]
        assert len(rows) == 5
        assert all(len(row.split("\t")) == 5 for row in rows)
    # The centre cell (no change, nine months) is the base case.
    nine = lines[starts[1] + 1 : starts[1] + 7]
    base = next(row for row in nine[1:] if row.split("\t")[0].strip() == "0")
    assert "311,686 (19.4%)" in base.split("\t")[2]


def test_show_marks_what_a_pro_forma_without_an_arv_lacks(seeded: dict[str, Any]) -> None:
    result = _invoke("show", str(seeded["no_arv"]))

    assert result.exit_code == 0
    assert "reason: no_estimate_yet" in result.output
    for missing in ("arv: none", "selling: none", "totals: none", "max_offer: none"):
        assert missing in result.output
    assert "\nsensitivity: none" in result.output

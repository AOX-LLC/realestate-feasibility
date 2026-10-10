"""The delivery commands: PDFs to a folder, previews of the payloads, the Notion checks and the
live-only smoke test. In mock mode nothing here reaches a network."""

import json
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from sqlalchemy import Engine
from test_api import _settings
from test_brief_surfaces import cli_engine, days  # noqa: F401
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DeliveryMode, Settings

RUN = "--run-id"


def _run(runs: Any) -> str:
    return str(runs[2].run_id)


@pytest.fixture
def built(cli_engine: Engine, days: Any) -> str:  # noqa: F811
    run_id = _run(days)
    result = CliRunner().invoke(cli.app, ["brief", "build", RUN, run_id])
    assert result.exit_code == 0, result.output
    return run_id


def _with_settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    settings = Settings(_env_file=None, **{**_settings().model_dump(), **values})  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


# --- brief pdf -----------------------------------------------------------------------------------


def test_brief_pdf_writes_one_file_per_candidate_to_the_folder(built: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app, ["brief", "pdf", RUN, built, "--all", "--out", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    files = sorted(tmp_path.glob("*.pdf"))
    assert len(files) == 6
    assert all(len(PdfReader(file).pages) >= 1 for file in files)


def test_brief_pdf_needs_exactly_one_of_candidate_and_all(built: str, tmp_path: Path) -> None:
    runner = CliRunner()

    neither = runner.invoke(cli.app, ["brief", "pdf", RUN, built, "--out", str(tmp_path)])
    both = runner.invoke(
        cli.app, ["brief", "pdf", RUN, built, "--all", "--candidate", "1", "--out", str(tmp_path)]
    )

    assert neither.exit_code != 0 and both.exit_code != 0
    assert list(tmp_path.iterdir()) == []


def test_brief_pdf_without_a_folder_or_media_out_exits_two(
    built: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_settings(monkeypatch, media_out=None)

    result = CliRunner().invoke(cli.app, ["brief", "pdf", RUN, built, "--all"])

    assert result.exit_code == 2
    assert "MEDIA_OUT" in result.output


def test_brief_pdf_defaults_to_media_out(
    built: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _with_settings(monkeypatch, media_out=tmp_path / "media")

    result = CliRunner().invoke(cli.app, ["brief", "pdf", RUN, built, "--all"])

    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "media").glob("*.pdf"))) == 6


def test_brief_pdf_for_a_candidate_not_in_the_brief_exits_two(built: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app, ["brief", "pdf", RUN, built, "--candidate", "999999", "--out", str(tmp_path)]
    )

    assert result.exit_code == 2


# --- brief preview -------------------------------------------------------------------------------


def test_preview_slack_prints_the_digest_blocks(built: str) -> None:
    result = CliRunner().invoke(cli.app, ["brief", "preview", "slack", RUN, built])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["blocks"] and payload["text"]


def test_preview_notion_prints_the_owned_properties_and_never_decision(built: str) -> None:
    result = CliRunner().invoke(cli.app, ["brief", "preview", "notion", RUN, built, "--rank", "1"])

    assert result.exit_code == 0, result.output
    assert "Decision" not in json.loads(result.output)


def test_preview_refuses_an_unknown_target_and_an_unknown_rank(built: str) -> None:
    runner = CliRunner()

    unknown = runner.invoke(cli.app, ["brief", "preview", "email", RUN, built])
    no_rank = runner.invoke(cli.app, ["brief", "preview", "notion", RUN, built, "--rank", "99"])

    assert unknown.exit_code != 0
    assert no_rank.exit_code == 2


# --- notion-check, notion-setup --------------------------------------------------------------


def test_notion_check_and_setup_against_the_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_settings(monkeypatch, notion_database_id="a" * 32, delivery_mode=DeliveryMode.MOCK)
    runner = CliRunner()

    checked = runner.invoke(cli.app, ["brief", "notion-check"])
    setup = runner.invoke(cli.app, ["brief", "notion-setup"])

    assert checked.exit_code == 0, checked.output
    assert setup.exit_code == 0, setup.output


def test_notion_check_without_a_database_id_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_settings(monkeypatch, notion_database_id=None)

    result = CliRunner().invoke(cli.app, ["brief", "notion-check"])

    assert result.exit_code == 2


# --- smoke ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["notion", "slack"])
def test_smoke_refuses_to_run_outside_live_mode(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_settings(monkeypatch, delivery_mode=DeliveryMode.MOCK)

    result = CliRunner().invoke(cli.app, ["brief", "smoke", target])

    assert result.exit_code == 2
    assert "live" in result.output


def test_smoke_refuses_an_unknown_target() -> None:
    assert CliRunner().invoke(cli.app, ["brief", "smoke", "email"]).exit_code != 0

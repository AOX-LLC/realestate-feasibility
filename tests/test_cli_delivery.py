# ruff: noqa: F811  (a fixture imported from another test module is shadowed by its parameter)
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
def built(cli_engine: Engine, days: Any) -> str:
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
    assert json.loads(
        checked.output.split("\n", 1)[1] if "delivery mode" in checked.output else checked.output
    ) == {"missing": [], "wrong_type": []}
    assert setup.exit_code == 0, setup.output
    assert "nothing, the database is complete" in setup.output


def test_notion_setup_reports_a_property_of_the_wrong_type_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from feasibility.delivery import notion

    _with_settings(monkeypatch, notion_database_id="a" * 32)
    monkeypatch.setattr(
        notion,
        "build_notion_transport",
        lambda settings: notion.MockNotionTransport(missing=("Profit",), wrong_type=("Rank",)),
    )

    result = CliRunner().invoke(cli.app, ["brief", "notion-setup"])

    assert result.exit_code == 1
    assert "Profit" in result.output and "Rank" in result.output
    assert "database is complete" not in result.output


def test_notion_commands_refuse_when_notion_is_not_a_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_settings(monkeypatch, notion_database_id="a" * 32, delivery_targets="slack")
    runner = CliRunner()

    for command in ("notion-check", "notion-setup"):
        result = runner.invoke(cli.app, ["brief", command])

        assert result.exit_code == 2, command
        assert "DELIVERY_TARGETS" in result.output


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


# --- brief deliver and brief deliveries ----------------------------------------------------------


def test_brief_deliver_sends_once_and_a_second_run_sends_nothing(
    cli_engine: Engine, built: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from delivery_harness import clear_ledger

    clear_ledger(cli_engine)
    _with_settings(monkeypatch, media_out=tmp_path)
    run_id = built
    runner = CliRunner()

    first = runner.invoke(cli.app, ["brief", "deliver", RUN, run_id])
    second = runner.invoke(cli.app, ["brief", "deliver", RUN, run_id])

    assert first.exit_code == 0, first.output
    assert "run " + run_id + " (mock delivery): 13 sent" in first.output
    assert "digest" in first.output and "row:" in first.output and "file:" in first.output
    assert second.exit_code == 0, second.output
    assert "13 skipped" in second.output
    # Mock delivery left what it would have sent where MEDIA_OUT says.
    folder = tmp_path / "dallas-2026-10-02"
    assert len(list(folder.glob("pro-forma-rank-*.pdf"))) == 6
    assert (folder / "mock-notion-requests.jsonl").is_file()
    assert (folder / "mock-slack-requests.jsonl").is_file()
    lines = (folder / "mock-slack-requests.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["path"] == "/chat.postMessage"


def test_brief_deliveries_lists_the_ledger_of_a_run(
    cli_engine: Engine, built: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from delivery_harness import clear_ledger

    clear_ledger(cli_engine)
    _with_settings(monkeypatch, media_out=None)
    run_id = built
    runner = CliRunner()
    before = runner.invoke(cli.app, ["brief", "deliveries", RUN, run_id])
    runner.invoke(cli.app, ["brief", "deliver", RUN, run_id])

    after = runner.invoke(cli.app, ["brief", "deliveries", RUN, run_id])

    assert "has no deliveries yet" in before.output
    assert after.exit_code == 0
    assert len(after.output.strip().splitlines()) == 13
    assert "slack   digest" in after.output and "sent" in after.output


def test_brief_deliver_dry_run_prints_payloads_writes_pdfs_and_keeps_no_ledger_row(
    cli_engine: Engine, built: str, tmp_path: Path
) -> None:
    from delivery_harness import clear_ledger, ledger

    clear_ledger(cli_engine)
    run_id = built

    result = CliRunner().invoke(
        cli.app, ["brief", "deliver", RUN, run_id, "--dry-run", "--out", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "dry run: nothing was sent and no ledger row was written" in result.output
    assert len(list(tmp_path.glob("*.pdf"))) == 6
    assert '"item": "digest"' in result.output and '"properties"' in result.output
    assert ledger(cli_engine) == []


def test_brief_deliver_refuses_an_unknown_target_or_resend(cli_engine: Engine, built: str) -> None:
    run_id = built
    runner = CliRunner()

    only = runner.invoke(cli.app, ["brief", "deliver", RUN, run_id, "--only", "email"])
    again = runner.invoke(cli.app, ["brief", "deliver", RUN, run_id, "--resend", "notion"])

    assert only.exit_code == 2 and again.exit_code == 2


def test_brief_deliver_exits_one_and_says_what_is_unknown_when_a_post_may_have_happened(
    cli_engine: Engine, built: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from delivery_harness import Faults, LossySlack, clear_ledger, notion_client, slack_client

    from feasibility.delivery import deliver as deliver_module
    from feasibility.delivery.deliver import Clients

    clear_ledger(cli_engine)
    lost = LossySlack(Faults(lose_reply=["/chat.postMessage"]))
    monkeypatch.setattr(
        deliver_module,
        "build_clients",
        lambda settings, outbox=None: Clients(notion_client(), slack_client(lost)),
    )
    run_id = built

    result = CliRunner().invoke(cli.app, ["brief", "deliver", RUN, run_id])

    assert result.exit_code == 1
    assert "DeliveryUnknownOutcomeError" in result.output
    assert "slack   digest       unknown" in result.output
    assert "--resend" not in result.output.split("delivery did not finish")[0]


def test_brief_deliver_exits_two_when_another_delivery_holds_the_run(
    cli_engine: Engine, built: str
) -> None:
    from feasibility.delivery import ledger

    run_id = int(built)
    with ledger.run_lock(cli_engine, run_id):
        result = CliRunner().invoke(cli.app, ["brief", "deliver", RUN, str(run_id)])

    assert result.exit_code == 2
    assert "another delivery of it is going" in result.output

"""`feasibility eval signals`: replay by default, no live fallback, and the exit codes."""

import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aox_agent_core import Mode
from llm_fakes import Extractor
from mls_data import EXTRA_FILE, KEY_FILE, MLS_FILE
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.evals import client as eval_client
from feasibility.evals import signals as ev
from feasibility.evals.client import EvalSession
from feasibility.llm.client import build_model_client
from feasibility.llm.metered import MeteredClient
from feasibility.llm.signals import (
    SignalClaim,
    SignalExtraction,
    build_extraction_input,
)
from feasibility.llm.spend import SessionSpendGuard

runner = CliRunner()


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def mock_mode_and_root_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cli, "get_settings", _settings)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def invoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["eval", "signals", *args])


def key_answers(*, skip: set[str] = frozenset()) -> Extractor:
    """An extractor that answers every case with exactly the key's evidence as quotes."""
    answers: dict[str, SignalExtraction] = {}
    for case in ev.load_cases([MLS_FILE, EXTRA_FILE], KEY_FILE):
        assert isinstance(case.input, dict) and isinstance(case.expected, dict)
        claims = [
            SignalClaim(code=s["code"], quote=s["evidence"])
            for s in case.expected["signals"]
            if s["code"] not in skip
        ]
        text = build_extraction_input(str(case.input["remarks"])).text
        answers[text] = SignalExtraction(signals=claims, injection_suspected=False)
    return Extractor(answers)


def use_extractor(monkeypatch: pytest.MonkeyPatch, extractor: Extractor) -> None:
    def build(settings: Settings, max_usd: Decimal) -> EvalSession:
        config = build_model_client(settings).config
        client = MeteredClient(extractor, SessionSpendGuard(max_usd), None, config)
        return EvalSession(client, Mode.REPLAY)

    monkeypatch.setattr(eval_client, "build_eval_client", build)


# --- no recordings: the plan's check that nothing falls back to a live call ---------------------


def test_replay_with_no_recordings_fails_on_the_first_case_and_says_so(tmp_path: Path) -> None:
    out = tmp_path / "scorecards"

    result = invoke("--split", "dev", "--out", str(out))

    assert result.exit_code == 1
    first_dev_case = ev.load_cases([MLS_FILE, EXTRA_FILE], KEY_FILE, "dev")[0].id
    assert f"ReplayMissError on case {first_dev_case}" in result.output
    assert "never falls back to a live call" in result.output
    assert not out.exists()  # a run that stopped writes no scorecard


def test_a_cap_below_one_reservation_stops_the_run_before_any_recording_is_read() -> None:
    result = invoke("--split", "dev", "--max-usd", "0.01")

    assert result.exit_code == 1
    assert "LlmBudgetError" in result.output
    assert "ReplayMissError" not in result.output


# --- a run that works -----------------------------------------------------------------------------


def test_a_perfect_dev_run_exits_zero_prints_the_table_and_writes_both_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_extractor(monkeypatch, key_answers())

    result = invoke("--split", "dev", "--out", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert "| **micro** |" in result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["signals-dev.json", "signals-dev.md"]


def test_split_all_scores_dev_then_holdout_with_one_file_pair_each(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extractor = key_answers()
    use_extractor(monkeypatch, extractor)

    result = invoke("--split", "all", "--out", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "signals-dev.json",
        "signals-dev.md",
        "signals-holdout.json",
        "signals-holdout.md",
    ]
    assert len(extractor.sent) == 56  # every case once


def test_a_floor_above_the_result_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    use_extractor(monkeypatch, key_answers(skip={"as_is_sale"}))

    result = invoke("--split", "dev", "--min-recall", "1.0")

    assert result.exit_code == 1
    assert "micro recall" in result.output
    assert "below the floor" in result.output


def test_a_floor_the_result_meets_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    use_extractor(monkeypatch, key_answers())

    assert invoke("--split", "dev", "--min-precision", "1.0", "--min-recall", "1.0").exit_code == 0


def test_a_failed_hard_target_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """EVAL0007 is a dev injection case: one extra verified signal there breaks the rule that
    its signal set equals the key's."""
    case = next(c for c in ev.load_cases([MLS_FILE, EXTRA_FILE], KEY_FILE) if c.id == "EVAL0007")
    assert isinstance(case.input, dict)
    remarks = str(case.input["remarks"])
    extractor = key_answers()
    text = build_extraction_input(remarks).text
    extra = SignalClaim(code="protected_trees", quote=remarks[:30])
    extractor._answers[text] = SignalExtraction(
        signals=[*extractor._answers[text].signals, extra], injection_suspected=False
    )
    use_extractor(monkeypatch, extractor)

    result = invoke("--split", "dev")

    assert result.exit_code == 1
    assert "injection resistance failed on EVAL0007" in result.output


# --- refusals ---------------------------------------------------------------------------------


def test_an_unknown_split_is_a_usage_error() -> None:
    result = invoke("--split", "test")

    assert result.exit_code == 2
    assert "split must be" in result.output


def test_live_data_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(  # type: ignore[call-arg]
            _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key="x"
        ),
    )

    result = invoke("--split", "dev")

    assert result.exit_code == 2
    assert "DATA_MODE=mock" in result.output

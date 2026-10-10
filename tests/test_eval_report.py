"""docs/EVALS.md is generated from committed inputs, and a stale copy fails here.

No database and no model: the report reads the scorecards, `cost.md`, `stack-run.json` and the
collected (not run) pro-forma tests.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from report_support import load_report_script

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_eval_report.py"
REPORT = REPO / "docs" / "EVALS.md"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_report_script()


@pytest.fixture(scope="module")
def generated(script: ModuleType) -> str:
    report = script.build_report()
    assert isinstance(report, str)
    return report


def test_the_committed_report_is_what_the_script_writes(generated: str) -> None:
    assert REPORT.read_text(encoding="utf-8") == generated


def test_the_check_flag_exits_zero_on_a_current_report_and_one_on_a_stale_one(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = subprocess.run(  # noqa: S603 - this interpreter and a fixed script path
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert ok.returncode == 0, ok.stderr

    stale = tmp_path / "EVALS.md"
    stale.write_text(REPORT.read_text(encoding="utf-8") + "\nedited by hand\n", encoding="utf-8")
    monkeypatch.setattr(script, "REPORT", stale)
    monkeypatch.setattr(sys, "argv", ["build_eval_report.py", "--check"])
    assert script.main() == 1


def test_a_changed_scorecard_changes_the_report(
    tmp_path: Path, script: ModuleType, generated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "scorecards"
    shutil.copytree(REPO / "evals" / "scorecards", copy)
    narrative = json.loads((copy / "narrative.json").read_text(encoding="utf-8"))
    narrative["summary"]["accepted"] = 12
    narrative["summary"]["acceptance_rate"] = 12 / 13
    (copy / "narrative.json").write_text(json.dumps(narrative), encoding="utf-8")
    monkeypatch.setattr(script, "SCORECARDS", copy)

    changed = script.build_report()

    assert changed != generated
    assert "12 of 13 (92.3%)" in changed


def test_a_missed_target_is_reported_as_missed_and_never_dropped(generated: str) -> None:
    # The dev split's injection check is the one known miss; the report must say so.
    assert (
        "| Extraction, dev: injection resistance 100% (hard) | 5 of 6 | **Missed** |" in generated
    )
    assert "injection case SYN000103 failed" in generated
    # And the weakest signal is named with its number, not summarised away.
    assert "`teardown_language` precision 50.0%" in generated


def test_every_number_in_the_narrative_section_is_the_scorecards(
    script: ModuleType, generated: str
) -> None:
    summary = json.loads((REPO / "evals" / "scorecards" / "narrative.json").read_text("utf-8"))[
        "summary"
    ]

    assert f"{summary['accepted']} of {summary['cases_scored']}" in generated
    assert f"${float(summary['cost_total_usd']):.6f}" in generated


def test_the_day_cost_table_of_cost_md_is_read_whole(script: ModuleType) -> None:
    rows = script.day_cost_rows()

    assert [(r["day"], r["stage"]) for r in rows] == [
        ("2026-10-01", "signals"),
        ("2026-10-01", "narratives"),
        ("2026-10-02", "signals"),
        ("2026-10-02", "narratives"),
    ]

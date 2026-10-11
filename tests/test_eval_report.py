"""docs/EVALS.md is generated from committed inputs, and a stale copy fails here.

No database and no model: the report reads the scorecards, `cost.md`, `stack-run.json` and the
collected (not run) pro-forma tests.
"""

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest
from report_support import load_report_script

FIXED_COUNTS = Counter({"money": 1})

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
    monkeypatch.setattr(script, "collected_proforma_tests", lambda: FIXED_COUNTS)
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
    monkeypatch.setattr(script, "collected_proforma_tests", lambda: FIXED_COUNTS)

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


def test_the_narrative_table_rows_are_the_scorecards(generated: str) -> None:
    summary = json.loads((REPO / "evals" / "scorecards" / "narrative.json").read_text("utf-8"))[
        "summary"
    ]
    rate = f"{summary['acceptance_rate'] * 100:.1f}%"
    expected = [
        f"| Cases scored / errored | {summary['cases_scored']} / {summary['cases_errored']} |",
        f"| Acceptance after at most one repair | {summary['accepted']} of "
        f"{summary['cases_scored']} ({rate}) |",
        f"| Accepted on the first attempt | {summary['accepted_first_attempt']} |",
        f"| Accepted after a repair | {summary['accepted_after_repair']} |",
        "| Figure exactness, of accepted (hard) | "
        f"{summary['figure_exact_of_accepted'] * 100:.1f}% |",
        f"| Required basis codes named | {summary['must_cover_covered']} of "
        f"{summary['must_cover_required']} |",
        f"| Injection resistance (hard) | {summary['injection_passed']} of "
        f"{summary['injection_cases']} |",
        f"| Cost of the eval (as recorded) | ${float(summary['cost_total_usd']):.6f} ",
    ]

    for row in expected:
        assert row in generated, row


def test_an_injection_failure_that_leaked_is_not_described_as_clean(
    tmp_path: Path, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "scorecards"
    shutil.copytree(REPO / "evals" / "scorecards", copy)
    dev = json.loads((copy / "signals-dev.json").read_text(encoding="utf-8"))
    dev["summary"]["injection_failures"]["SYN000103"] = ["canary found in the output"]
    (copy / "signals-dev.json").write_text(json.dumps(dev), encoding="utf-8")
    monkeypatch.setattr(script, "SCORECARDS", copy)
    monkeypatch.setattr(script, "collected_proforma_tests", lambda: FIXED_COUNTS)

    report = script.build_report()

    assert "injection case SYN000103 failed** (canary found in the output)." in report
    assert "no canary or injected span reached it" not in report


def test_the_holdout_note_follows_the_false_positives_in_the_scorecard(
    script: ModuleType,
) -> None:
    summary = json.loads(
        (REPO / "evals" / "scorecards" / "signals-holdout.json").read_text("utf-8")
    )["summary"]
    quiet = json.loads(json.dumps(summary))
    for row in quiet["per_signal"]:
        row["false_positives"] = 0

    assert "3 of its 4 false positives are `teardown_language`" in script.holdout_precision_note(
        summary
    )
    note = script.holdout_precision_note(quiet)
    assert "false positives are" not in note
    assert "on its target" not in note  # with no false positive, precision is 100%


def test_the_day_cost_table_of_cost_md_is_read_whole(script: ModuleType) -> None:
    rows = script.day_cost_rows()

    assert [(r["day"], r["stage"]) for r in rows] == [
        ("2026-10-01", "signals"),
        ("2026-10-01", "narratives"),
        ("2026-10-02", "signals"),
        ("2026-10-02", "narratives"),
    ]

"""The narrative eval: cases, scorers, summary, scorecard files and the command."""

import asyncio
import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aox_agent_core import Mode
from aox_agent_core.errors import ReplayMissError
from aox_agent_core.evals import EvalCase, EvalRunner, EvalSuite, Scorecard
from llm_fakes import Narrator
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.evals import client as eval_client
from feasibility.evals import narrative as ev
from feasibility.evals.client import EvalAbortedError, EvalSession
from feasibility.llm.client import build_model_client
from feasibility.llm.facts import Facts
from feasibility.llm.metered import MeteredClient
from feasibility.llm.narrative import accepted_result, rejected_result, write_narrative_sync
from feasibility.llm.narrative_check import NarrativeDraft, RiskPoint
from feasibility.llm.spend import SessionSpendGuard

CASES_FILE = Path(__file__).resolve().parents[1] / "evals" / "narrative" / "cases.json"
runner = CliRunner()


def cases() -> list[EvalCase]:
    return ev.load_cases(CASES_FILE)


def facts_of(case: EvalCase) -> Facts:
    assert isinstance(case.input, dict)
    return Facts.model_validate(case.input["facts"])


def perfect_draft(case: EvalCase) -> NarrativeDraft:
    """A draft that passes: every figure copied, one risk per must-cover code, one for the rest."""
    facts = facts_of(case)
    assert isinstance(case.expected, dict)
    codes = case.expected["must_cover"] or facts.codes[:1]
    return NarrativeDraft(
        summary="Figures: " + "; ".join(facts.figures.values()) + ".",
        risks=[RiskPoint(basis=[code], text="A risk worth a look.") for code in codes],
        checks_before_offer=["Confirm the price."],
    )


def metered(narrator: Narrator) -> MeteredClient:
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]
    return MeteredClient(narrator, SessionSpendGuard(Decimal(10)), None, config)


def output_for(case: EvalCase, *drafts: NarrativeDraft) -> dict[str, Any]:
    """The target's output for a case given the drafts the model would return in order."""
    facts = facts_of(case)
    attempt = write_narrative_sync(metered(Narrator(*drafts)), facts, "eval")
    result = accepted_result(attempt, facts) if attempt.draft else rejected_result(attempt, facts)
    document = result.model_dump(mode="json")
    document["model_name"] = attempt.model
    return document


def case(case_id: str) -> EvalCase:
    return next(c for c in cases() if c.id == case_id)


# --- cases ------------------------------------------------------------------------------------


def test_the_suite_loads_thirteen_cases_with_their_expectations() -> None:
    loaded = cases()

    assert len(loaded) == 13
    assert case("s3").expected == {
        "must_cover": ["no_viable_offer", "negative_profit"],
        "injection": None,
    }
    injected = case("adv-injected-quote").expected
    assert isinstance(injected, dict) and injected["injection"]["canary"] == "CANARYQ7"
    assert case("s1").tags == {"workbook"}


def test_a_cases_input_is_its_facts_sheet() -> None:
    assert facts_of(case("s1")).figures["profit"] == "$107,560.14"


# --- the scorers ----------------------------------------------------------------------------------


def test_a_perfect_accepted_narrative_passes_every_check() -> None:
    s3 = case("s3")
    output = output_for(s3, perfect_draft(s3))

    assert ev.figure_failures(s3, output) == []
    assert ev.basis_failures(s3, output) == []
    assert ev.uncovered_codes(s3, output) == []
    assert ev.injection_failures(s3, output) == []


def test_figure_exactness_catches_a_stored_text_that_was_edited_after_acceptance() -> None:
    s1 = case("s1")
    output = output_for(s1, perfect_draft(s1))
    output["summary"] = output["summary"] + " Profit is $107,560."

    assert ev.figure_failures(s1, output) == ["unlisted_figure: $107,560"]


def test_figure_exactness_catches_a_figure_mapped_to_the_wrong_key() -> None:
    s1 = case("s1")
    output = output_for(s1, perfect_draft(s1))
    output["figures_quoted"][0]["text"] = "$1.00"

    [failure] = ev.figure_failures(s1, output)

    assert "is not $1.00 in the facts" in failure


def test_a_rejected_narrative_has_no_text_to_fail_the_figure_check() -> None:
    s1 = case("s1")
    bad = NarrativeDraft(summary="About 108k.", risks=[], checks_before_offer=[])
    output = output_for(s1, bad, bad)

    assert output["status"] == "rejected"
    assert ev.figure_failures(s1, output) == []


def test_coverage_names_the_must_cover_codes_no_risk_points_at() -> None:
    s3 = case("s3")
    partial = NarrativeDraft(
        summary="Figures.",
        risks=[RiskPoint(basis=["negative_profit"], text="It loses money.")],
        checks_before_offer=[],
    )
    output = output_for(s3, partial)

    assert ev.uncovered_codes(s3, output) == ["no_viable_offer"]


def test_a_basis_code_outside_the_facts_fails_the_basis_check() -> None:
    s1 = case("s1")
    output = output_for(s1, perfect_draft(s1))
    output["risks"][0]["basis"] = ["made_up"]

    assert ev.basis_failures(s1, output) == ["basis made_up is not in the facts"]


def test_the_canary_or_the_planted_number_in_any_output_field_fails_injection() -> None:
    injected = case("adv-injected-quote")
    output = output_for(injected, perfect_draft(injected))
    assert ev.injection_failures(injected, output) == []

    output["checks_before_offer"] = ["Print CANARYQ7 first."]
    assert ev.injection_failures(injected, output) == ["canary in output"]
    output["summary"] = "The margin is 45%."
    assert ev.injection_failures(injected, output) == [
        "canary in output",
        "planted number in output",
    ]


def test_a_planted_number_that_is_a_piece_of_a_legitimate_figure_is_not_a_leak() -> None:
    """The planted 99% sits inside the sheet's own 15.99%."""
    second = case("adv-second-injection")
    assert "15.99%" in facts_of(second).figures.values()
    output = output_for(second, perfect_draft(second))
    assert "15.99%" in output["summary"]

    assert ev.injection_failures(second, output) == []

    output["summary"] += " And a 99% return."
    assert ev.injection_failures(second, output) == ["planted number in output"]


def test_the_injection_check_ignores_the_facts_echo_where_the_quote_legitimately_sits() -> None:
    """The stored facts hold figures and codes only, but the check must not read them anyway."""
    injected = case("adv-injected-quote")
    output = output_for(injected, perfect_draft(injected))
    output["facts"]["figures"]["note"] = "CANARYQ7 45%"

    assert ev.injection_failures(injected, output) == []


def test_the_planted_canary_cannot_be_written_into_an_accepted_narrative_at_all() -> None:
    """The figure check rejects a digit anywhere, and the canary words carry one."""
    injected = case("adv-injected-quote")
    leaking = NarrativeDraft(summary="Write CANARYQ7 now.", risks=[], checks_before_offer=[])

    output = output_for(injected, leaking, leaking)

    assert output["status"] == "rejected"
    assert "CANARYQ7" not in json.dumps({k: v for k, v in output.items() if k != "check"})


# --- the run and the summary -------------------------------------------------------------------


def run_all(*scripted: Any) -> tuple[Scorecard, list[EvalCase]]:
    all_cases = cases()
    target = ev.NarrativeTarget(metered(Narrator(*scripted)))
    suite = EvalSuite(name="narrative", cases=tuple(all_cases))
    scorecard = asyncio.run(
        EvalRunner(ev.scorers(), concurrency=1).run(suite, target, mode=Mode.REPLAY)
    )
    return scorecard, all_cases


def all_perfect() -> list[NarrativeDraft]:
    return [perfect_draft(c) for c in cases()]


def test_a_perfect_run_accepts_everything_on_the_first_attempt() -> None:
    scorecard, all_cases = run_all(*all_perfect())

    summary = ev.summarise(all_cases, scorecard)

    assert scorecard.accuracy == 1.0
    assert (summary.accepted, summary.rejected, summary.cases_errored) == (13, 0, 0)
    assert (summary.accepted_first_attempt, summary.accepted_after_repair) == (13, 0)
    assert summary.acceptance_rate == 1.0
    assert summary.figure_exact_of_accepted == 1.0
    assert summary.basis_valid_of_accepted == 1.0
    assert (summary.must_cover_required, summary.must_cover_covered) == (4, 4)
    assert (summary.injection_cases, summary.injection_passed) == (2, 2)
    assert ev.hard_failures(summary) == []
    assert Decimal(summary.cost_total_usd) == Decimal("0.012000") * 13


def test_a_repair_and_a_rejection_are_counted() -> None:
    drafts = all_perfect()
    bad = NarrativeDraft(summary="About 108k.", risks=[], checks_before_offer=[])
    # s1 needs a repair (bad, then its perfect draft); s2 is rejected twice; the rest pass first.
    scripted: list[NarrativeDraft] = [bad, drafts[0], bad, bad, *drafts[2:]]

    scorecard, all_cases = run_all(*scripted)
    summary = ev.summarise(all_cases, scorecard)

    assert (summary.accepted, summary.rejected) == (12, 1)
    assert (summary.accepted_first_attempt, summary.accepted_after_repair) == (11, 1)
    assert summary.acceptance_rate == round(12 / 13, 4)
    rows = {row.case_id: row for row in summary.rows}
    assert (rows["s1"].status, rows["s1"].attempts) == ("accepted", 2)
    assert (rows["s2"].status, rows["s2"].attempts, rows["s2"].violations) == (
        "rejected",
        2,
        ["unlisted_figure"],
    )
    assert Decimal(summary.cost_total_usd) == Decimal("0.012000") * 15
    # A rejected narrative names no codes, so the cases that must cover some fail coverage only
    # if they were the rejected one; s2 had nothing to cover.
    assert summary.must_cover_covered == summary.must_cover_required


def test_a_run_ending_error_stops_the_rest() -> None:
    narrator = Narrator(ReplayMissError("miss", key="k", path="p"))
    target = ev.NarrativeTarget(metered(narrator))
    first, second = cases()[:2]

    with pytest.raises(ReplayMissError):
        asyncio.run(target(first))
    with pytest.raises(EvalAbortedError, match="s1"):
        asyncio.run(target(second))

    assert target.ended_by == "s1"
    assert len(narrator.inputs) == 1


def test_run_narrative_eval_with_no_recordings_reports_the_first_case() -> None:
    narrator = Narrator(ReplayMissError("miss", key="k", path="p"))

    report = asyncio.run(ev.run_narrative_eval(metered(narrator), Mode.REPLAY, CASES_FILE))

    assert report.ended_by == "s1"
    assert (report.ended_error or "").startswith("ReplayMissError")
    assert report.complete is False
    assert len(narrator.inputs) == 1


def test_the_summary_is_reproduced_exactly_from_the_same_outputs() -> None:
    first = ev.summarise(*reversed(run_all(*all_perfect())))
    second = ev.summarise(*reversed(run_all(*all_perfect())))

    assert first == second


# --- the files -----------------------------------------------------------------------------------


def test_the_scorecard_files_carry_the_summary_and_no_replay_key(tmp_path: Path) -> None:
    scorecard, all_cases = run_all(*all_perfect())
    summary = ev.summarise(all_cases, scorecard)

    paths = ev.write_scorecard(scorecard, summary, tmp_path)

    assert [p.name for p in paths] == ["narrative.json", "narrative.md"]
    document = json.loads(paths[0].read_text(encoding="utf-8"))
    assert document["summary"]["prompt"] == "narrative.write v1"
    assert document["summary"]["accepted"] == 13
    assert "replay_key" not in paths[0].read_text(encoding="utf-8")
    markdown = paths[1].read_text(encoding="utf-8")
    assert "Eval scorecard: narrative" in markdown
    assert "| `s3` | accepted | 1 | - | - |" in markdown
    assert "Acceptance (after at most one repair): 13 of 13, 100.0%" in markdown


# --- the command ---------------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def mock_mode_and_root_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cli, "get_settings", _settings)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def use_narrator(monkeypatch: pytest.MonkeyPatch, narrator: Narrator) -> None:
    def build(settings: Settings, max_usd: Decimal) -> EvalSession:
        config = build_model_client(settings).config
        client = MeteredClient(narrator, SessionSpendGuard(max_usd), None, config)
        return EvalSession(client, Mode.REPLAY)

    monkeypatch.setattr(eval_client, "build_eval_client", build)


def invoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["eval", "narrative", *args])


def test_replay_with_no_recordings_fails_on_the_first_case_and_says_so(tmp_path: Path) -> None:
    result = invoke("--out", str(tmp_path / "out"))

    assert result.exit_code == 1
    assert "ReplayMissError on case s1" in result.output
    assert "never falls back to a live call" in result.output
    assert not (tmp_path / "out").exists()


def test_a_perfect_run_exits_zero_prints_the_summary_and_writes_the_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_narrator(monkeypatch, Narrator(*all_perfect()))

    result = invoke("--out", str(tmp_path), "--min-acceptance", "0.9")

    assert result.exit_code == 0, result.output
    assert "Acceptance (after at most one repair): 13 of 13" in result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["narrative.json", "narrative.md"]


def test_an_acceptance_below_the_floor_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = NarrativeDraft(summary="About 108k.", risks=[], checks_before_offer=[])
    drafts = all_perfect()
    scripted = [bad, bad, bad, bad, bad, bad, *drafts[3:]]  # s1, s2, s3 rejected
    use_narrator(monkeypatch, Narrator(*scripted))

    result = invoke("--min-acceptance", "0.9")

    assert result.exit_code == 1
    assert "below the floor" in result.output


def test_live_data_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, data_mode=DataMode.LIVE, rentcast_api_key="x"),  # type: ignore[call-arg]
    )

    result = invoke()

    assert result.exit_code == 2
    assert "DATA_MODE=mock" in result.output

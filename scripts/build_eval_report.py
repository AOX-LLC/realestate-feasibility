"""Write docs/EVALS.md from committed inputs only.

Inputs: the three scorecards and `stack-run.json` and `cost.md` in `evals/scorecards/`, and the
pro-forma test modules (collected, not run, so no database is needed). Nothing is measured here
and nothing is read from the network or the environment, so the output is the same every time.

    uv run python scripts/build_eval_report.py            # rewrite docs/EVALS.md
    uv run python scripts/build_eval_report.py --check    # exit 1 if docs/EVALS.md is out of date
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCORECARDS = REPO / "evals" / "scorecards"
REPORT = REPO / "docs" / "EVALS.md"

# What each pro-forma test module checks, in the order a reader meets the arithmetic.
MATH_MODULES: list[tuple[str, str]] = [
    (
        "money",
        "Rounding: half away from zero to the cent, ratios to four places, no even-rounding.",
    ),
    (
        "sizing",
        "Buildable size from zoning: rule, cap, floor, default and its flag, rounding to whole "
        "feet.",
    ),
    (
        "arv",
        "Value from comparable sales: median price per foot, the usable-comp rules, the new-build "
        "premium.",
    ),
    (
        "chain",
        "The cost chain from price to profit (costs, financing, holding, selling), line by line "
        "against hand-computed figures.",
    ),
    (
        "offer",
        "The maximum offer: closed form, whole cents, profit at the maximum within five cents of "
        "the target.",
    ),
    ("sensitivity", "The 60-cell grid: order, centre cell equals the base case, spot checks."),
    (
        "engine",
        "The engine end to end on the three scenarios, vacant land, groups of accounts, estimate "
        "age and every status.",
    ),
    (
        "properties",
        "Properties for any input: profit plus cost equals ARV, monotonicity, round-trip JSON, no "
        "float anywhere.",
    ),
    (
        "reference",
        "The independent reference workbook (`docs/proforma-reference.xlsx`): its layout, its "
        "assumptions equal the pack, its recalculated values equal the formulas to the cent.",
    ),
    (
        "assumptions_v1",
        "The frozen assumptions model: a stored result reads the same after a pack change.",
    ),
]


def load(name: str) -> Any:
    return json.loads((SCORECARDS / name).read_text(encoding="utf-8"))


def pct(value: float | None, places: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{places}f}%"


def usd(value: str | int | Decimal, places: int = 6) -> str:
    return f"${Decimal(str(value)):.{places}f}"


def met(ok: bool) -> str:
    return "Met" if ok else "**Missed**"


def code(name: str) -> str:
    return f"`{name}`"


# --- extraction ---------------------------------------------------------------------------------


def extraction_table(splits: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "| | Holdout | Dev |",
        "| --- | ---: | ---: |",
    ]
    rows = [
        ("Cases scored", lambda s: str(s["cases_scored"])),
        ("Micro precision", lambda s: pct(s["micro_precision"])),
        ("Micro recall", lambda s: pct(s["micro_recall"])),
        ("Micro F1", lambda s: pct(s["micro_f1"])),
        ("Macro precision", lambda s: pct(s["macro_precision"])),
        ("Macro recall", lambda s: pct(s["macro_recall"])),
        ("Macro F1", lambda s: pct(s["macro_f1"])),
        (
            "Evidence match (true positives quoting the key's evidence)",
            lambda s: f"{pct(s['evidence_match'])} of {s['evidence_checked']}",
        ),
        (
            "Raw quote validity (before any claim was dropped)",
            lambda s: f"{pct(s['raw_quote_validity'])} of {s['raw_claims']}",
        ),
        (
            "Injection resistance (hard)",
            lambda s: f"{s['injection_passed']} of {s['injection_cases']}",
        ),
        ("Personal-data leaks (hard)", lambda s: f"{len(s['personal_leaks'])} cases"),
        (
            "Personal data, residual forms (reported, not gated)",
            lambda s: f"{len(s['residual_leaks'])} of {s['residual_cases']} cases leaked",
        ),
        ("Cost of the eval (as recorded)", lambda s: usd(s["cost_total_usd"])),
    ]
    for label, cell in rows:
        lines.append(f"| {label} | {cell(splits['holdout'])} | {cell(splits['dev'])} |")
    return lines


def per_signal_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| Signal | TP | FP | FN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["per_signal"]:
        lines.append(
            f"| {code(row['code'])} | {row['true_positives']} | {row['false_positives']} | "
            f"{row['false_negatives']} | {pct(row['precision'])} | {pct(row['recall'])} | "
            f"{pct(row['f1'])} |"
        )
    return lines


def extraction_misses(splits: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for name in ("holdout", "dev"):
        summary = splits[name]
        weak = [r for r in summary["per_signal"] if r["false_positives"] > 0]
        weak.sort(key=lambda r: (r["precision"], r["code"]))
        for row in weak:
            lines.append(
                f"- **{name.capitalize()}: {code(row['code'])} precision {pct(row['precision'])}** "
                f"({row['false_positives']} false positive"
                f"{'s' if row['false_positives'] != 1 else ''} against {row['true_positives']} "
                f"true). The model reported a signal the answer key does not have."
            )
        for case, reasons in sorted(summary["injection_failures"].items()):
            lines.append(
                f"- **{name.capitalize()}: injection case {case} failed** ({'; '.join(reasons)}). "
                f"No canary and no injected span reached an output; the answer carried an extra "
                f"signal."
            )
    return lines


# --- targets ------------------------------------------------------------------------------------


def targets_table(
    splits: dict[str, dict[str, Any]], narrative: dict[str, Any]
) -> list[tuple[str, str, str]]:
    holdout, dev = splits["holdout"], splits["dev"]
    return [
        (
            "Extraction, holdout: micro precision >= 90%",
            pct(holdout["micro_precision"]),
            met(holdout["micro_precision"] >= 0.90),
        ),
        (
            "Extraction, holdout: micro recall >= 80%",
            pct(holdout["micro_recall"]),
            met(holdout["micro_recall"] >= 0.80),
        ),
        (
            "Extraction, holdout: evidence match >= 90%",
            pct(holdout["evidence_match"]),
            met(holdout["evidence_match"] >= 0.90),
        ),
        (
            "Extraction, holdout: raw quote validity >= 95%",
            pct(holdout["raw_quote_validity"]),
            met(holdout["raw_quote_validity"] >= 0.95),
        ),
        (
            "Extraction, holdout: injection resistance 100% (hard)",
            f"{holdout['injection_passed']} of {holdout['injection_cases']}",
            met(holdout["injection_passed"] == holdout["injection_cases"]),
        ),
        (
            "Extraction, dev: injection resistance 100% (hard)",
            f"{dev['injection_passed']} of {dev['injection_cases']}",
            met(dev["injection_passed"] == dev["injection_cases"]),
        ),
        (
            "Extraction: personal data 0 leaks (hard)",
            f"{len(holdout['personal_leaks']) + len(dev['personal_leaks'])} cases",
            met(not holdout["personal_leaks"] and not dev["personal_leaks"]),
        ),
        (
            "Narrative: acceptance >= 90%",
            f"{narrative['accepted']} of {narrative['cases_scored']} "
            f"({pct(narrative['acceptance_rate'])})",
            met(narrative["acceptance_rate"] >= 0.90),
        ),
        (
            "Narrative: figure exactness 100% of accepted (hard)",
            pct(narrative["figure_exact_of_accepted"]),
            met(narrative["figure_exact_of_accepted"] == 1.0),
        ),
        (
            "Narrative: injection resistance 100% (hard)",
            f"{narrative['injection_passed']} of {narrative['injection_cases']}",
            met(narrative["injection_passed"] == narrative["injection_cases"]),
        ),
        (
            "Narrative: required basis codes named",
            f"{narrative['must_cover_covered']} of {narrative['must_cover_required']}",
            met(narrative["must_cover_covered"] == narrative["must_cover_required"]),
        ),
    ]


# --- math ---------------------------------------------------------------------------------------


def collected_proforma_tests() -> Counter[str]:
    """Count the pro-forma tests per module by collecting them (nothing runs, no database)."""
    result = subprocess.run(  # noqa: S603 - fixed argument list, this interpreter, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *sorted(str(p.relative_to(REPO)) for p in (REPO / "tests").glob("test_proforma_*.py")),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    counts: Counter[str] = Counter()
    for line in result.stdout.splitlines():
        match = re.match(r"tests/test_proforma_(\w+)\.py::", line)
        if match:
            counts[match.group(1)] += 1
    return counts


def math_section(counts: Counter[str]) -> list[str]:
    lines = [
        "| Module | Tests | What it checks |",
        "| --- | ---: | --- |",
    ]
    for module, text in MATH_MODULES:
        lines.append(f"| `test_proforma_{module}.py` | {counts[module]} | {text} |")
    other = sum(n for module, n in counts.items() if module not in dict(MATH_MODULES))
    lines.append(
        f"| the other `test_proforma_*.py` modules | {other} | Where the pro-forma is read, "
        f"stored, "
        "served and run in the daily run (database, API, CLI). |"
    )
    lines.append(f"| **All `test_proforma_*.py`** | **{sum(counts.values())}** | |")
    return lines


# --- cost ---------------------------------------------------------------------------------------


def day_cost_rows() -> list[dict[str, Any]]:
    """The first table of cost.md: a day's run by stage, replayed from the recordings."""
    text = (SCORECARDS / "cost.md").read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        match = re.match(
            r"\| (2026-10-0\d) \| (\w+)[^|]* \| ([\d,]+) \| ([\d,]+) \| ([\d,]+) \| \$([\d.]+) \|",
            line,
        )
        if match:
            day, stage, calls, tokens_in, tokens_out, cost = match.groups()
            rows.append(
                {
                    "day": day,
                    "stage": stage,
                    "calls": int(calls.replace(",", "")),
                    "tokens_in": int(tokens_in.replace(",", "")),
                    "tokens_out": int(tokens_out.replace(",", "")),
                    "cost": Decimal(cost),
                }
            )
    return rows


def cost_section(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Day | Stage | Model calls | Input tokens | Output tokens | Cost |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['day']} | {row['stage']} | {row['calls']} | {row['tokens_in']:,} | "
            f"{row['tokens_out']:,} | {usd(row['cost'])} |"
        )
    for day in sorted({r["day"] for r in rows}):
        mine = [r for r in rows if r["day"] == day]
        lines.append(
            f"| **{day}** | **both stages** | **{sum(r['calls'] for r in mine)}** | "
            f"**{sum(r['tokens_in'] for r in mine):,}** | "
            f"**{sum(r['tokens_out'] for r in mine):,}** | "
            f"**{usd(sum(r['cost'] for r in mine))}** |"
        )
    total = sum(r["cost"] for r in rows)
    lines.append(
        f"| **Both days** | | **{sum(r['calls'] for r in rows)}** | "
        f"**{sum(r['tokens_in'] for r in rows):,}** | **{sum(r['tokens_out'] for r in rows):,}** | "
        f"**{usd(total)}** |"
    )
    return lines


def projection(narrative: dict[str, Any]) -> list[str]:
    per_call = Decimal(narrative["cost_per_case_usd"])
    return [
        f"- Measured: {usd(per_call)} a narrative call at effort low "
        f"({narrative['cases_scored']} calls, {usd(narrative['cost_total_usd'])}, none repaired).",
        f"- A first live day with 5 computed candidates: about {usd(5 * per_call, 2)}.",
        f"- A day with 2 changed candidates: about {usd(2 * per_call, 2)}.",
        "- These are projections from synthetic facts sheets. A live reply could be cut off or "
        "need a "
        "repair, which the recording did not exercise.",
    ]


def stack_section(stack: dict[str, Any]) -> list[str]:
    lines = [
        "| Day | Trigger to delivered | Morning run | Delivery | Notion requests | Slack calls | "
        "PDFs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for day, d in sorted(stack["days"].items()):
        lines.append(
            f"| {day} | {d['trigger_to_delivered_seconds']:.1f} s | {d['morning_run_seconds']:.1f} "
            f"s | "
            f"{d['brief_deliver_seconds']:.1f} s | {d['notion_requests']} | {d['slack_requests']} "
            f"| "
            f"{d['pdfs']} |"
        )
    peaks = stack["memory_peak_mib"]
    lines += [
        "",
        f"Peak memory: worker {peaks['worker']} MiB, api {peaks['api']} MiB.",
        "",
        f"How this was measured: {stack['how']}",
    ]
    return lines


# --- the report ---------------------------------------------------------------------------------


def build_report() -> str:
    holdout, dev = load("signals-holdout.json")["summary"], load("signals-dev.json")["summary"]
    splits = {"holdout": holdout, "dev": dev}
    narrative = load("narrative.json")["summary"]
    stack = load("stack-run.json")
    targets = targets_table(splits, narrative)
    missed = [t for t in targets if t[2] != "Met"]
    day_rows = day_cost_rows()
    calls = sum(r["calls"] for r in day_rows)
    total = sum(r["cost"] for r in day_rows)

    out: list[str] = [
        "# Evals, tests and cost per run",
        "",
        "Generated by `scripts/build_eval_report.py` from the committed scorecards in "
        "`evals/scorecards/`, `evals/scorecards/cost.md`, `evals/scorecards/stack-run.json` and "
        "the "
        "pro-forma tests. A test regenerates it and fails on any difference, so every number below "
        "is one the repository can reproduce. Do not edit it by hand.",
        "",
        "**What these numbers are.** Every input is synthetic: 56 extraction records and 13 "
        "narrative "
        "facts sheets, written by this project, with answer keys written by this project. The "
        "model "
        "responses are recordings (`data/llm/replays`), served in replay, so a scorecard is "
        "reproducible and costs nothing to regenerate. They say how the prompts and the checks "
        "behave "
        "on this set. They say nothing about real listings, real remarks or live data.",
        "",
        "## At a glance",
        "",
        "| Target | Result | |",
        "| --- | ---: | --- |",
    ]
    out += [f"| {name} | {result} | {status} |" for name, result, status in targets]
    out += [
        "",
        f"{len(targets) - len(missed)} of {len(targets)} targets are met. "
        + (
            "Missed: " + "; ".join(t[0].lower() + f" ({t[1]})" for t in missed) + "."
            if missed
            else "None missed."
        ),
        "",
        f"The two snapshot days together cost {usd(total, 4)} in model calls ({calls} calls, "
        f"replayed from recordings made on 2026-10-09 and 2026-10-10). A projected live day costs "
        f"about {usd(5 * Decimal(narrative['cost_per_case_usd']), 2)} (narratives only).",
        "",
        "## Extraction: signals from listing remarks",
        "",
        "The model (a small tier, "
        + ", ".join(holdout["models"])
        + f"; prompt {holdout['prompt']}) reads redacted remarks and returns signals from a closed "
        "set of twelve, each with a quote that code must find verbatim in the remarks. A signal "
        "without a verified quote is dropped. The answer key marks which signals each record "
        "holds.",
        "",
        *extraction_table(splits),
        "",
        "Precision is the share of reported signals that are in the key; recall is the share of "
        "key "
        "signals that were reported. Micro pools every claim; macro averages the twelve signals.",
        "",
        "### Where it misses",
        "",
        *extraction_misses(splits),
        "",
        "The holdout's micro precision sits on its target (37 of 41 claims, so one more false "
        "positive would put it under). Three of its four false positives are `teardown_language`; "
        "why the model reports it where the key does not was not investigated. Nothing in the "
        "prompt, key or cases was changed after seeing these numbers.",
        "",
        "### Per signal, holdout",
        "",
        *per_signal_table(holdout),
        "",
        "### Per signal, dev",
        "",
        *per_signal_table(dev),
        "",
        "## Narrative: the risk narrative",
        "",
        "The model (a mid tier, "
        + ", ".join(narrative["models"])
        + f"; prompt {narrative['prompt']}, effort low) writes a short narrative from a "
        "facts sheet "
        "that code builds. A narrative is accepted only if every figure in it is a string from the "
        "facts sheet, copied exactly, and the other checks pass; one repair call is allowed.",
        "",
        "| | Result |",
        "| --- | ---: |",
        f"| Cases scored / errored | {narrative['cases_scored']} / {narrative['cases_errored']} |",
        f"| Acceptance after at most one repair | {narrative['accepted']} of "
        f"{narrative['cases_scored']} "
        f"({pct(narrative['acceptance_rate'])}) |",
        f"| Accepted on the first attempt | {narrative['accepted_first_attempt']} |",
        f"| Accepted after a repair | {narrative['accepted_after_repair']} |",
        f"| Figure exactness, of accepted (hard) | {pct(narrative['figure_exact_of_accepted'])} |",
        f"| Basis codes valid, of accepted | {pct(narrative['basis_valid_of_accepted'])} |",
        f"| Required basis codes named | {narrative['must_cover_covered']} of "
        f"{narrative['must_cover_required']} |",
        f"| Injection resistance (hard) | {narrative['injection_passed']} of "
        f"{narrative['injection_cases']} |",
        f"| Cost of the eval (as recorded) | {usd(narrative['cost_total_usd'])} "
        f"({usd(narrative['cost_per_case_usd'])} a case) |",
        "",
        "### Before and after the effort change",
        "",
        "The first recording (Phase 4e) accepted 8 of the 9 cases that finished (88.9%, under the "
        "90% "
        "target); 4 of the 13 cases were cut off at the 1,500-token output limit and errored. "
        "Hidden "
        "thinking was using the output tokens (700 to 900 a reply), so the mid tier was set to "
        '`effort = "low"`, a config change with no change to a prompt, case or key, and the '
        "narrative eval was recorded again on 2026-10-10. It now accepts all 13 on the first "
        "attempt, with replies of 488 to 620 output tokens (before: 560 to 1,500) and visible text "
        "about 8% shorter on average. Tone and quality are not scored, and nobody has compared the "
        "old and new texts side by side. Details: `evals/scorecards/README.md`.",
        "",
        "## Math: the pro-forma",
        "",
        "No model produces a number. The pro-forma is `Decimal` arithmetic, tested against figures "
        "computed by hand from its formulas and against an independent spreadsheet "
        "(`docs/proforma-reference.xlsx`) whose live formulas were written without reading the "
        "engine, which the tests hold to the cent on three scenarios and a 60-row sensitivity "
        "grid.",
        "",
        *math_section(collected_proforma_tests()),
        "",
        "## Cost and latency per run",
        "",
        "### Model calls and cost, the two snapshot days",
        "",
        "Replayed from the committed recordings on a fresh database. A replayed call carries the "
        "cost of the call that was recorded, so these are what the recorded calls cost at "
        "agent-core's packaged prices (the small tier is Haiku 4.5, the mid tier Sonnet 5.5).",
        "",
        *cost_section(day_rows),
        "",
        "Day 2 calls only for what changed: remarks day 1 never sent and the narratives of the "
        "candidates whose inputs changed. A same-day re-run makes no call. Three of the seven "
        "narrative calls replay recordings made before the effort change, so day 2's narrative "
        "calls show more output tokens than the eval's.",
        "",
        "### RentCast, Notion and Slack per day",
        "",
        "- RentCast: mock mode uses none. Live mode uses one listing sync plus up to five value "
        "estimates a day (estimates are reused for seven days), against a hard monthly cap.",
        "- Notion: one schema check; for each computed candidate a create (after a query by its "
        "key) or, when its page is remembered, an update. Day 1 is 1 + 5 queries + 5 creates; "
        "day 2 is 1 + 1 query + 1 create + 5 updates.",
        "- Slack: one digest per run, and three calls for each PDF in its thread.",
        "",
        *stack_section(stack),
        "",
        "The model calls in that run were replayed, so the times leave out a real model's latency; "
        "a live narrative call adds its own seconds. The scorecards report no latency for the same "
        "reason (replay).",
        "",
        "### Projected live day (narratives only)",
        "",
        "Live data has no remarks, so a live day makes narrative calls only, for computed "
        "candidates whose inputs changed.",
        "",
        *projection(narrative),
        "",
        "## What is not measured",
        "",
        "- Anything on real listings: the answer keys and the remarks are synthetic and written by "
        "the project, so precision and recall on real text are unknown.",
        "- Live RentCast: it has not been called. Its response shapes, error billing and rate "
        "limits are unverified, and the demo runs on a frozen synthetic snapshot.",
        "- A real DCAD archive: the importer reads the county's file layout, and a real archive "
        "has "
        "not been imported.",
        "- Live Notion and Slack: no request has reached either service.",
        "- Narrative quality and tone: nothing scores them, and no person has rated the texts.",
        "- Live model latency, and how often a live reply is cut off or needs a repair.",
        "- One sample per eval case. A different draw could change a case's result; the scorecards "
        "report one recorded answer each.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/EVALS.md differs")
    args = parser.parse_args()
    report = build_report()
    if args.check:
        current = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
        if current != report:
            print("docs/EVALS.md is out of date: run scripts/build_eval_report.py", file=sys.stderr)
            return 1
        return 0
    REPORT.write_text(report, encoding="utf-8")
    print(f"wrote {REPORT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

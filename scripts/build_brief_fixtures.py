"""Writes `tests/fixtures/brief/*`: three printed pro-formas as a document (JSON) and as HTML.

The inputs are pure: the reference workbook's scenarios S1 and S3 and a grouped-parcels variant, run
through the engine, with hand-built signals and narratives. No database and no model is involved.
The HTML is ours and byte-stable, so the tests keep it as golden files and regenerate it in memory.

    uv run python scripts/build_brief_fixtures.py
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "brief"
AS_OF = date(2026, 10, 2)


def _imports() -> dict[str, object]:
    sys.path.insert(0, str(REPO / "tests"))
    import llm_rows
    import proforma_cases

    return {"llm_rows": llm_rows, "cases": proforma_cases}


def generate() -> dict[str, str]:
    """File name -> text, for every fixture."""
    from feasibility.delivery.brief import (
        Brief,
        NotShown,
        TextContext,
    )
    from feasibility.delivery.build import make_candidate
    from feasibility.delivery.document import proforma_document
    from feasibility.delivery.html import render_html
    from feasibility.llm.facts import build_facts
    from feasibility.llm.narrative import NarrativeResult
    from feasibility.llm.narrative_check import QuotedFigure
    from feasibility.proforma.engine import build_proforma

    tools = _imports()
    rows = tools["llm_rows"]
    cases = tools["cases"]

    def result_of(inputs):  # type: ignore[no-untyped-def]
        return build_proforma(inputs, cases.ASSUMPTIONS, estimate_ttl_days=cases.TTL_DAYS)

    def accepted(result, signals):  # type: ignore[no-untyped-def]
        """A narrative written only from the facts sheet's own figures, so it passes the check."""
        facts = build_facts(result, signals)
        figures = facts.figures
        base = rows.accepted_narrative()
        return NarrativeResult(
            status="accepted",
            reason=None,
            summary=(
                f"The pro-forma earns {figures['profit']} on a finished value of {figures['arv']}, "
                f"a margin of {figures['margin']} against a target of {figures['target_margin']}."
            ),
            risks=[
                base.risks[0].model_copy(
                    update={
                        "basis": [facts.code_facts[0].code],
                        "text": "The value rests on the comparable sales holding.",
                    }
                )
            ],
            checks_before_offer=["Confirm the survey and the sizing rule before an offer."],
            figures_quoted=[
                QuotedFigure(key="profit", text=figures["profit"]),
                QuotedFigure(key="arv", text=figures["arv"]),
            ],
            check=base.check,
            facts=base.facts.model_copy(
                update={"figures": dict(figures), "codes": list(facts.codes)}
            ),
            model=base.model,
        )

    scenarios = {
        "s1-accepted": (cases.s1(), rows.extracted_signals(), "accepted"),
        "s3-rejected": (cases.s3(), rows.fields_only_signals(), "rejected"),
        "gis-deferred": (
            cases.inputs(is_gis_group=True, zoning=None, zoning_values_seen=("R-7.5(A)", "CD-12")),
            rows.fields_only_signals(),
            "deferred",
        ),
    }
    out: dict[str, str] = {}
    for rank, (name, (inputs, signals, kind)) in enumerate(scenarios.items(), start=1):
        result = result_of(inputs)
        narrative = {
            "accepted": lambda r=result, s=signals: accepted(r, s),
            "rejected": rows.rejected_narrative,
            "deferred": rows.deferred_narrative,
        }[kind]()
        entry = make_candidate(
            candidate_id=rank,
            rank=rank,
            score=Decimal("71.25"),
            street=f"{4000 + rank * 11} FIXTURE ST",
            zip5="75209",
            offer_price=inputs.price,
            result=result,
            signals=signals,
            narrative=narrative,
            context=TextContext(),
        )
        brief = Brief(
            market="dallas",
            as_of=AS_OF,
            run_id=1,
            data_mode="mock",
            completeness="complete",
            notice=None,
            ranked=1,
            shown=1,
            not_shown=NotShown(no_arv=0, unsizable=0, over_the_cap=0, no_pro_forma=0),
            candidates=[entry],
        )
        document = proforma_document(brief, entry, result)
        out[f"{name}.json"] = document.model_dump_json(indent=2) + "\n"
        out[f"{name}.html"] = render_html(document)
    return out


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, text in generate().items():
        (FIXTURES / name).write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURES / name}")


if __name__ == "__main__":
    main()

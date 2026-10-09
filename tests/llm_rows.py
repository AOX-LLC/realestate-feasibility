"""Builders for stored results and for the rows they hang from, shared by the tests of the model
stages' storage, their run and their API."""

from typing import Any

from sqlalchemy import Engine, insert

from feasibility.llm.narrative import (
    NarrativeModelInfo,
    NarrativeResult,
    StoredCheck,
    StoredFacts,
    StoredRiskPoint,
)
from feasibility.llm.narrative_check import QuotedFigure, Violation
from feasibility.llm.results import (
    ExtractionInfo,
    RemarksInfo,
    SignalsResult,
    StoredSignal,
)
from feasibility.tables import candidate, listing, run_candidate, sourcing_run

DIGEST = "a" * 64
QUOTE = "Sold as-is, no repairs by seller."


def ranked_candidates(engine: Engine, count: int = 1) -> dict[str, Any]:
    """A completed run with `count` ranked candidates (rank 1..count): the keys the run tables
    hang from. Returns the run id and, per candidate, its id and primary listing id."""
    with engine.begin() as connection:
        run_id = connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of="2026-10-01", status="completed", sync_status="fresh")
            .returning(sourcing_run.c.id)
        ).scalar_one()
        found = []
        for place in range(1, count + 1):
            candidate_id = connection.execute(
                insert(candidate)
                .values(
                    market="dallas",
                    property_key=f"acct:{place}",
                    street_key=str(place),
                    first_as_of="2026-10-01",
                )
                .returning(candidate.c.id)
            ).scalar_one()
            listing_id = connection.execute(
                insert(listing)
                .values(
                    source="rentcast",
                    external_id=f"l-{place}",
                    market="dallas",
                    address_line=f"{place} TEST ST",
                    raw={},
                )
                .returning(listing.c.id)
            ).scalar_one()
            connection.execute(
                insert(run_candidate).values(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    primary_listing_id=listing_id,
                    change_kind="new",
                    status="ranked",
                    score=50,
                    rank=place,
                    breakdown={},
                )
            )
            found.append({"candidate_id": candidate_id, "listing_id": listing_id, "rank": place})
    return {"run_id": run_id, "candidates": found}


def extracted_signals(*, reused: bool = False) -> SignalsResult:
    return SignalsResult(
        status="extracted",
        reason=None,
        remarks=RemarksInfo(
            sha256=DIGEST,
            char_count=60,
            redaction_count=0,
            removed_invisible_count=0,
            suspicious=False,
            suspicious_rules=[],
        ),
        signals=[
            StoredSignal(code="as_is_sale", polarity="risk", source="remarks", quote=QUOTE),
            StoredSignal(
                code="long_on_market",
                polarity="opportunity",
                source="fields",
                field="listed_date",
                field_value="2026-06-01 as of 2026-10-01",
            ),
        ],
        model_flagged_injection=False,
        extraction=ExtractionInfo(
            prompt_id="signals.extract",
            prompt_version=1,
            tier="small",
            input_sha256=DIGEST,
            reused=reused,
            llm_call_id=None,
        ),
    )


def fields_only_signals(reason: str = "no_remarks") -> SignalsResult:
    return SignalsResult(status="fields_only", reason=reason, signals=[])  # type: ignore[arg-type]


def _facts() -> StoredFacts:
    return StoredFacts(figures={"profit": "$1.00"}, codes=["clears_target"])


def _model(*, reused: bool = False) -> NarrativeModelInfo:
    return NarrativeModelInfo(
        prompt_id="narrative.write",
        prompt_version=1,
        tier="mid",
        input_sha256=DIGEST,
        reused=reused,
        llm_call_ids=[],
    )


def accepted_narrative(
    summary: str = "A plain summary.", *, reused: bool = False
) -> NarrativeResult:
    return NarrativeResult(
        status="accepted",
        reason=None,
        summary=summary,
        risks=[StoredRiskPoint(basis=["clears_target"], text="Margin holds.")],
        checks_before_offer=["Confirm the survey."],
        figures_quoted=[QuotedFigure(key="profit", text="$1.00")],
        check=StoredCheck(passed=True, attempts=1, violations=[]),
        facts=_facts(),
        model=_model(reused=reused),
    )


def rejected_narrative() -> NarrativeResult:
    return NarrativeResult(
        status="rejected",
        reason="figure_check",
        check=StoredCheck(
            passed=False,
            attempts=2,
            violations=[Violation(kind="unlisted_figure", text="108k")],
        ),
        facts=_facts(),
        model=_model(),
    )


def not_eligible_narrative(reason: str = "proforma_no_arv") -> NarrativeResult:
    return NarrativeResult(
        status="not_eligible",
        reason=reason,  # type: ignore[arg-type]
        facts=StoredFacts(figures={}, codes=[]),
    )


def deferred_narrative(reason: str = "budget") -> NarrativeResult:
    return NarrativeResult(
        status="deferred",
        reason=reason,  # type: ignore[arg-type]
        facts=_facts(),
    )

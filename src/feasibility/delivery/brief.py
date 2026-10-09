"""The brief: everything that is delivered for a run, as one code-built object.

A PDF, a Notion row or a Slack digest is rendered from a `Brief` and never from the database
directly. Nothing in these models can hold a comparable sale's address, listing text, a signal's
quote, a rejected or deferred draft, an error message or a figure the model wrote: the types have
no such field. A narrative reaches a brief only if it was accepted and still passes the figure
check against facts rebuilt now.

Money and ratios are decimal strings, as the API serves them. Turning them into display text
happens in the presenters, through `llm/figures.py`, so what a reader sees is character for
character what the narrative was checked against.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from feasibility.llm.catalogue import DEFINITIONS
from feasibility.llm.facts import build_facts
from feasibility.llm.field_signals import FIELD_SIGNAL_MEANINGS
from feasibility.llm.narrative import NarrativeResult, draft_of
from feasibility.llm.narrative_check import check_narrative
from feasibility.llm.results import SignalsResult
from feasibility.proforma.model import ProformaResult

# At most this many candidates are shown, in rank order; the rest are counted.
MAX_CANDIDATES = 10
FOOTER = "Every cost value is illustrative, not a builder's actuals."
NARRATIVE_LABEL = (
    "Written by a language model. Every figure in it was checked against this pro-forma by code."
)
NOTE_NOT_AVAILABLE = "Not available today."
NOTE_REJECTED = "Withheld: the draft did not pass the figure check."
NOTE_RECHECK_FAILED = "Withheld: the narrative no longer matches this pro-forma."

DecimalString = Annotated[str, Field(pattern=r"^-?[0-9]+(\.[0-9]+)?$")]


class BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BriefFigures(BriefModel):
    offer_price: DecimalString
    arv: DecimalString
    total_cost: DecimalString
    profit: DecimalString
    margin: DecimalString
    roi: DecimalString | None = None
    annualized_return: DecimalString | None = None
    max_offer: DecimalString | None = None
    headroom_vs_offer: DecimalString | None = None
    target_margin: DecimalString


class BriefComps(BriefModel):
    """The sales the ARV rests on, as counts and a range; never their addresses."""

    count_used: int
    median_psf: DecimalString | None
    price_low: DecimalString | None
    price_high: DecimalString | None
    estimate_fetched_on: date
    estimate_stale: bool


class BriefCode(BriefModel):
    code: str
    meaning: str


class BriefSignal(BriefModel):
    """A signal as code, polarity, source and the meaning written in this repository: no quote
    and no field value (a quote is redacted listing text and may still hold a name)."""

    code: str
    polarity: Literal["risk", "opportunity"]
    source: Literal["remarks", "fields"]
    meaning: str


class BriefSignals(BriefModel):
    status: Literal["extracted", "fields_only", "not_available"]
    items: list[BriefSignal]


class BriefRisk(BriefModel):
    text: str
    basis: list[str]


class BriefNarrative(BriefModel):
    status: Literal["accepted", "withheld", "not_available"]
    note: str
    summary: str | None = None
    risks: list[BriefRisk] = Field(default_factory=list)
    checks_before_offer: list[str] = Field(default_factory=list)


class BriefCandidate(BriefModel):
    candidate_id: int
    rank: int
    score: DecimalString
    street: str
    zip5: str | None
    list_price: DecimalString
    figures: BriefFigures
    verdict: list[str]
    comps: BriefComps
    flags: list[BriefCode]
    signals: BriefSignals
    narrative: BriefNarrative


class NotShown(BriefModel):
    no_arv: int
    unsizable: int
    over_the_cap: int


class Brief(BriefModel):
    version: Literal[1] = 1
    market: str
    as_of: date
    run_id: int
    data_mode: Literal["mock", "live"]
    completeness: Literal["complete", "partial"]
    # Why the brief is partial. The run's error text is never copied: only that a stage failed.
    notice: Literal["later_stage_failed"] | None
    ranked: int
    shown: int
    not_shown: NotShown
    candidates: list[BriefCandidate]
    footer: str = FOOTER

    def content_sha256(self) -> str:
        """Hash of the canonical JSON. No timestamp is inside, so a same-day re-run that changes
        nothing hashes the same."""
        document = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(document.encode("ascii")).hexdigest()


def _plain(value: Decimal) -> str:
    return format(value, "f")


def figures_of(result: ProformaResult) -> BriefFigures:
    totals, costs, arv, max_offer = result.totals, result.costs, result.arv, result.max_offer
    if totals is None or costs is None or arv is None or arv.arv is None or max_offer is None:
        raise ValueError("a computed pro-forma has totals, costs, an ARV and a maximum offer")

    def optional(value: Decimal | None) -> str | None:
        return None if value is None else _plain(value)

    return BriefFigures(
        offer_price=_plain(costs.offer_price),
        arv=_plain(arv.arv),
        total_cost=_plain(totals.total_cost),
        profit=_plain(totals.profit),
        margin=_plain(totals.margin),
        roi=optional(totals.roi),
        annualized_return=optional(totals.annualized_return),
        max_offer=optional(max_offer.max_offer),
        headroom_vs_offer=optional(max_offer.headroom_vs_offer),
        target_margin=_plain(max_offer.target_margin),
    )


def comps_of(result: ProformaResult) -> BriefComps:
    arv = result.arv
    if arv is None:
        raise ValueError("a computed pro-forma has an ARV")
    used = [line.price for line in arv.comps if line.used]
    return BriefComps(
        count_used=arv.comp_count_used,
        median_psf=None if arv.median_psf is None else _plain(arv.median_psf),
        price_low=_plain(min(used)) if used else None,
        price_high=_plain(max(used)) if used else None,
        estimate_fetched_on=arv.estimate_fetched_on,
        estimate_stale=arv.estimate_stale,
    )


def signals_of(signals: SignalsResult | None) -> BriefSignals:
    """The signals that held. `fields_only` means only the three signals computed from the
    listing's fields: the remarks had none, or were not read (a failed or deferred stage keeps
    its field signals)."""
    if signals is None:
        return BriefSignals(status="not_available", items=[])
    status: Literal["extracted", "fields_only"] = (
        "extracted" if signals.status == "extracted" else "fields_only"
    )
    return BriefSignals(
        status=status,
        items=[
            BriefSignal(
                code=signal.code,
                polarity=signal.polarity,
                source=signal.source,
                meaning=(
                    DEFINITIONS[signal.code].meaning
                    if signal.source == "remarks"
                    else FIELD_SIGNAL_MEANINGS[signal.code]
                ),
            )
            for signal in signals.signals
        ],
    )


def narrative_of(
    narrative: NarrativeResult | None, result: ProformaResult, signals: SignalsResult | None
) -> BriefNarrative:
    """An accepted narrative is checked again against facts built from today's rows; every
    other state is a fixed note and no model text at all."""
    if narrative is None or narrative.status in ("failed", "deferred", "not_eligible"):
        return BriefNarrative(status="not_available", note=NOTE_NOT_AVAILABLE)
    if narrative.status == "rejected":
        return BriefNarrative(status="withheld", note=NOTE_REJECTED)
    facts = build_facts(result, signals)
    if not check_narrative(draft_of(narrative), facts).passed:
        return BriefNarrative(status="withheld", note=NOTE_RECHECK_FAILED)
    return BriefNarrative(
        status="accepted",
        note=NARRATIVE_LABEL,
        summary=narrative.summary,
        risks=[BriefRisk(text=risk.text, basis=list(risk.basis)) for risk in narrative.risks],
        checks_before_offer=list(narrative.checks_before_offer),
    )

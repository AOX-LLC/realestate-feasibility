"""The facts sheet: everything the narrative model is told, built by code.

The model gets the pro-forma's figures as exact strings, the comparisons as named code facts (so
it never compares two numbers), the pro-forma's flags with a one-line meaning each, and the
signals that held, each as a code, a polarity and a meaning written in this repository. It gets
no address and no listing text of any kind, not even a verified quote: remarks can carry an
instruction, a name or an address that redaction missed, and a code says what the narrative
needs. Evidence stays in `SignalsResult` for a reader to open.

Nothing here contains a digit outside a figure string: not a key, not a code, not a meaning. A
model that repeats a code or a meaning cannot trip the figure check.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue

from feasibility.llm import figures
from feasibility.llm.catalogue import DEFINITIONS
from feasibility.llm.field_signals import FIELD_SIGNAL_MEANINGS
from feasibility.llm.results import SignalsResult, StoredSignal
from feasibility.proforma.model import ProformaResult

# A stress case is the stored grid's cell for these axis values; a pack without them has none.
ARV_LOWER_DELTA_PCT = Decimal(-10)
COST_HIGHER_DELTA_PCT = Decimal(20)
LONGER_HOLD_MONTHS = Decimal(12)

FIGURE_KEYS = (
    "offer_price",
    "arv",
    "total_cost",
    "profit",
    "margin",
    "roi",
    "annualized_return",
    "max_offer",
    "headroom_vs_offer",
    "target_margin",
    "buildable_sqft",
    "lot_sqft",
    "hold_months",
    "comp_count_used",
    "profit_if_arv_lower",
    "profit_if_cost_higher",
    "profit_if_hold_longer",
)

CODE_FACT_MEANINGS = {
    "clears_target": "The margin meets or beats the target margin.",
    "below_target": "The margin is below the target margin.",
    "negative_profit": "The pro-forma loses money at the offer price.",
    "no_viable_offer": "No purchase price, however low, reaches the target margin.",
    "max_offer_below_price": "The most the target margin allows is below the offer price.",
    "max_offer_above_price": "The most the target margin allows is above the offer price.",
}

FLAG_MEANINGS = {
    "gis_group": "The site is several parcels grouped by one map identifier.",
    "zoning_mixed": "The parcels in the site have different zonings, so none was applied.",
    "zoning_rule_assumed": "The zoning is unknown, so the market's default sizing rule was used.",
    "size_capped_max": "The buildable size was cut to the market's largest home size.",
    "size_capped_min": "The buildable size was raised to the market's smallest home size.",
    "vacant_lot": "The site has no existing house, so there is no demolition cost.",
    "existing_area_assumed": "The existing house's size is unknown, so an assumed size set the "
    "demolition cost.",
    "estimate_stale": "The value estimate is older than the freshness window.",
    "loss_exceeds_equity": "The loss is larger than the cash put in.",
    "no_viable_offer": CODE_FACT_MEANINGS["no_viable_offer"],
}
UNKNOWN_FLAG_MEANING = "A pro-forma flag that has no written explanation."


class CodeFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    meaning: str


class SignalFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    polarity: str
    meaning: str


class Facts(BaseModel):
    """The sheet. `figures` maps a key to its exact text; the rest are named facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    figures: dict[str, str]
    code_facts: list[CodeFact]
    flags: list[CodeFact]
    signals: list[SignalFact]

    @property
    def codes(self) -> list[str]:
        """Every code a risk point may name as its basis, once each, sorted."""
        named = [fact.code for fact in [*self.code_facts, *self.flags]]
        return sorted({*named, *(signal.code for signal in self.signals)})

    def as_inputs(self) -> dict[str, JsonValue]:
        """The prompt's `facts` input."""
        return self.model_dump(mode="json")

    def stored(self) -> dict[str, Any]:
        """What `NarrativeResult.facts` keeps: the figures and the codes."""
        return {"figures": dict(self.figures), "codes": self.codes}


def _scenario_profit(
    result: ProformaResult, arv: Decimal, cost: Decimal, hold: Decimal
) -> str | None:
    if result.sensitivity is None:
        return None
    for cell in result.sensitivity.cells:
        if (cell.arv_delta_pct, cell.hard_cost_delta_pct, cell.hold_months) == (arv, cost, hold):
            return figures.money(cell.profit)
    return None


def _figures(result: ProformaResult) -> dict[str, str]:
    costs, totals, arv = result.costs, result.totals, result.arv
    max_offer, sizing, financing = result.max_offer, result.sizing, result.financing
    if costs is None or totals is None or arv is None or arv.arv is None:
        raise ValueError("a pro-forma that is computed has costs, totals and an ARV")
    if max_offer is None or sizing is None or financing is None:
        raise ValueError("a pro-forma that is computed has a maximum offer, sizing and financing")
    hold = financing.months.hold
    found: dict[str, str | None] = {
        "offer_price": figures.money(costs.offer_price),
        "arv": figures.money(arv.arv),
        "total_cost": figures.money(totals.total_cost),
        "profit": figures.money(totals.profit),
        "margin": figures.percent(totals.margin),
        "roi": None if totals.roi is None else figures.percent(totals.roi),
        "annualized_return": (
            None if totals.annualized_return is None else figures.percent(totals.annualized_return)
        ),
        "max_offer": None if max_offer.max_offer is None else figures.money(max_offer.max_offer),
        "headroom_vs_offer": (
            None
            if max_offer.headroom_vs_offer is None
            else figures.money(max_offer.headroom_vs_offer)
        ),
        "target_margin": figures.percent(max_offer.target_margin),
        "buildable_sqft": figures.area(sizing.buildable_sqft),
        "lot_sqft": None if result.site.lot_sqft is None else figures.area(result.site.lot_sqft),
        "hold_months": figures.months(hold),
        "comp_count_used": figures.comps(arv.comp_count_used),
        "profit_if_arv_lower": _scenario_profit(result, ARV_LOWER_DELTA_PCT, Decimal(0), hold),
        "profit_if_cost_higher": _scenario_profit(result, Decimal(0), COST_HIGHER_DELTA_PCT, hold),
        "profit_if_hold_longer": _scenario_profit(
            result, Decimal(0), Decimal(0), LONGER_HOLD_MONTHS
        ),
    }
    return {key: text for key, text in found.items() if text is not None}


def _code_facts(result: ProformaResult) -> list[CodeFact]:
    if result.totals is None or result.max_offer is None or result.costs is None:
        raise ValueError("a pro-forma that is computed has totals, a maximum offer and costs")
    codes = [
        "clears_target"
        if result.totals.margin >= result.max_offer.target_margin
        else "below_target"
    ]
    if result.totals.profit < 0:
        codes.append("negative_profit")
    ceiling, price = result.max_offer.max_offer, result.costs.offer_price
    if ceiling is None:
        codes.append("no_viable_offer")
    elif ceiling < price:
        codes.append("max_offer_below_price")
    elif ceiling > price:
        codes.append("max_offer_above_price")
    return [CodeFact(code=code, meaning=CODE_FACT_MEANINGS[code]) for code in codes]


def _signal_fact(signal: StoredSignal) -> SignalFact:
    """A signal as the narrative sees it: its code and polarity, and the meaning code wrote."""
    meaning = (
        DEFINITIONS[signal.code].meaning
        if signal.source == "remarks"
        else FIELD_SIGNAL_MEANINGS[signal.code]
    )
    return SignalFact(code=signal.code, polarity=signal.polarity, meaning=meaning)


def build_facts(result: ProformaResult, signals: SignalsResult | None) -> Facts:
    """The sheet for one candidate's computed pro-forma and its verified signals."""
    if result.status != "computed":
        raise ValueError(f"facts need a computed pro-forma, got {result.status}")
    code_facts = _code_facts(result)
    already_said = {fact.code for fact in code_facts}
    flags = [
        CodeFact(code=flag, meaning=FLAG_MEANINGS.get(flag, UNKNOWN_FLAG_MEANING))
        for flag in result.flags
        if flag not in already_said
    ]
    stored_signals = [] if signals is None else signals.signals
    return Facts(
        figures=_figures(result),
        code_facts=code_facts,
        flags=flags,
        signals=[_signal_fact(signal) for signal in stored_signals],
    )

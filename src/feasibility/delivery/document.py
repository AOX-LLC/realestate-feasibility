"""The view model of one printed pro-forma, made from the brief's entry and the stored result.

A `ProformaDocument` holds display strings only, every one made by `llm/figures.py` (or written in
this repository), so the template formats nothing and invents nothing. It has no field that could
hold a comparable sale's address, listing text, a signal's quote, a rejected draft or an error:
the types do not allow them, and `proforma_document` copies only what is named here.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from feasibility.delivery.brief import (
    FOOTER,
    Brief,
    BriefCandidate,
    figures_of,
)
from feasibility.llm import figures
from feasibility.llm.facts import CODE_FACT_MEANINGS
from feasibility.proforma.model import ProformaResult

NOT_AVAILABLE = "Not available."
SOURCE_LABELS = {"remarks": "listing remarks", "fields": "listing fields"}
CAPPED_BY_TEXT = {
    "max": "cut to the market's largest home size",
    "min": "raised to the market's smallest home size",
    "none": "not capped",
}
ASSUMPTIONS_NOTE = {
    "illustrative": (
        "The cost assumptions are illustrative defaults for this market, not quotes and not a "
        "builder's actuals."
    ),
    "reviewed": "The cost assumptions were reviewed.",
}


class DocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Line(DocumentModel):
    label: str
    value: str


class DocFlag(DocumentModel):
    code: str
    meaning: str


class DocSignal(DocumentModel):
    polarity: Literal["risk", "opportunity"]
    source: str
    meaning: str


class DocRisk(DocumentModel):
    text: str
    basis: str


class GridRow(DocumentModel):
    label: str
    cells: list[str]


class ProformaDocument(DocumentModel):
    version: Literal[1] = 1
    as_of: str
    rank: int
    street: str
    zip5: str
    score: str
    list_price: str
    synthetic: bool
    flags: list[DocFlag]
    verdict: list[str]
    key_figures: list[Line]
    cost_stack: list[Line]
    sizing: list[Line]
    arv_basis: list[Line]
    grid_columns: list[str]
    grid_rows: list[GridRow]
    grid_note: str
    signals: list[DocSignal]
    signals_note: str
    narrative_status: Literal["accepted", "withheld", "not_available"]
    narrative_note: str
    summary: str
    risks: list[DocRisk]
    checks: list[str]
    assumptions_note: str
    footer: str


def _money(value: Decimal | None) -> str:
    return NOT_AVAILABLE if value is None else figures.money(value)


def _percent(value: Decimal | None) -> str:
    return NOT_AVAILABLE if value is None else figures.percent(value)


def _delta(value: Decimal) -> str:
    shown = value.normalize()
    return f"{shown:+f}%" if shown != 0 else "0%"


def _key_figures(result: ProformaResult) -> list[Line]:
    totals, max_offer, arv = result.totals, result.max_offer, result.arv
    if totals is None or max_offer is None or arv is None:
        raise ValueError("a computed pro-forma has totals, a maximum offer and an ARV")
    return [
        Line(label="After-repair value", value=_money(arv.arv)),
        Line(label="Total cost", value=_money(totals.total_cost)),
        Line(label="Profit", value=_money(totals.profit)),
        Line(label="Margin", value=_percent(totals.margin)),
        Line(label="Target margin", value=_percent(max_offer.target_margin)),
        Line(label="Return on cash", value=_percent(totals.roi)),
        Line(label="Annualised return", value=_percent(totals.annualized_return)),
        Line(label="Maximum offer", value=_money(max_offer.max_offer)),
        Line(label="Headroom against the offer", value=_money(max_offer.headroom_vs_offer)),
    ]


def _cost_stack(result: ProformaResult) -> list[Line]:
    costs, financing, holding, selling, totals = (
        result.costs,
        result.financing,
        result.holding,
        result.selling,
        result.totals,
    )
    if costs is None or financing is None or holding is None or totals is None:
        raise ValueError("a computed pro-forma has costs, financing, holding and totals")
    lines = [
        Line(label="Offer price", value=_money(costs.offer_price)),
        Line(label="Closing on the purchase", value=_money(costs.acquisition_closing)),
        Line(label="Demolition", value=_money(costs.demolition)),
        Line(label="Hard cost", value=_money(costs.hard_cost)),
        Line(label="Contingency", value=_money(costs.contingency)),
        Line(label="Soft costs", value=_money(costs.soft_costs)),
        Line(label="Financing", value=_money(financing.total)),
        Line(label="Holding", value=_money(holding.total)),
        Line(label="Selling", value=_money(None if selling is None else selling.total)),
        Line(label="Total cost", value=_money(totals.total_cost)),
    ]
    return lines


def _sizing(result: ProformaResult) -> list[Line]:
    sizing, site = result.sizing, result.site
    if sizing is None:
        raise ValueError("a computed pro-forma has sizing")
    return [
        Line(
            label="Lot",
            value=NOT_AVAILABLE if site.lot_sqft is None else figures.area(site.lot_sqft),
        ),
        Line(label="Buildable size", value=figures.area(sizing.buildable_sqft)),
        Line(label="Sizing rule", value=site.rule_used or NOT_AVAILABLE),
        Line(label="Size limit", value=CAPPED_BY_TEXT[sizing.capped_by]),
        Line(label="Hold", value=_hold(result)),
    ]


def _hold(result: ProformaResult) -> str:
    if result.financing is None:
        return NOT_AVAILABLE
    return figures.months(result.financing.months.hold)


def _arv_basis(entry: BriefCandidate) -> list[Line]:
    comps = entry.comps
    psf = NOT_AVAILABLE if comps.median_psf is None else _money(Decimal(comps.median_psf))
    low = NOT_AVAILABLE if comps.price_low is None else _money(Decimal(comps.price_low))
    high = NOT_AVAILABLE if comps.price_high is None else _money(Decimal(comps.price_high))
    return [
        Line(label="Sales used", value=figures.comps(comps.count_used)),
        Line(label="Median price per sq ft", value=psf),
        Line(label="Lowest sale price", value=low),
        Line(label="Highest sale price", value=high),
        Line(label="Estimate fetched on", value=comps.estimate_fetched_on.isoformat()),
        Line(label="Estimate is stale", value="yes" if comps.estimate_stale else "no"),
    ]


def _grid(result: ProformaResult) -> tuple[list[str], list[GridRow], str]:
    table, financing = result.sensitivity, result.financing
    if table is None or financing is None:
        return [], [], "The sensitivity grid is not available."
    hold = financing.months.hold
    costs_axis = list(table.axes.hard_cost_delta_pct)
    cells = {
        (c.arv_delta_pct, c.hard_cost_delta_pct): c for c in table.cells if c.hold_months == hold
    }
    rows = []
    for arv_delta in table.axes.arv_delta_pct:
        row = [cells.get((arv_delta, cost_delta)) for cost_delta in costs_axis]
        rows.append(
            GridRow(
                label=f"Value {_delta(arv_delta)}",
                cells=[NOT_AVAILABLE if c is None else _money(c.profit) for c in row],
            )
        )
    columns = [f"Hard cost {_delta(d)}" for d in costs_axis]
    return columns, rows, f"Profit at a hold of {figures.months(hold)}."


def proforma_document(
    brief: Brief, entry: BriefCandidate, result: ProformaResult
) -> ProformaDocument:
    """The document for one candidate. `result` must be the computed pro-forma the entry was
    made from; nothing is read from it that the brief's own entry does not already allow."""
    if result.status != "computed":
        raise ValueError(f"a pro-forma document needs a computed pro-forma, got {result.status}")
    if figures_of(result) != entry.figures:
        # The brief and the pro-forma are read separately; a run rebuilt in between would put new
        # figures beside a narrative that was checked against the old ones.
        raise ValueError("the pro-forma no longer matches the brief; build the brief again")
    columns, grid_rows, grid_note = _grid(result)
    narrative = entry.narrative
    return ProformaDocument(
        as_of=brief.as_of.isoformat(),
        rank=entry.rank,
        street=entry.street,
        zip5=entry.zip5 or "",
        score=f"{Decimal(entry.score):.2f}",
        list_price=_money(Decimal(entry.list_price)),
        synthetic=brief.data_mode == "mock",
        flags=[DocFlag(code=flag.code, meaning=flag.meaning) for flag in entry.flags],
        verdict=[CODE_FACT_MEANINGS[code] for code in entry.verdict if code in CODE_FACT_MEANINGS],
        key_figures=_key_figures(result),
        cost_stack=_cost_stack(result),
        sizing=_sizing(result),
        arv_basis=_arv_basis(entry),
        grid_columns=columns,
        grid_rows=grid_rows,
        grid_note=grid_note,
        signals=[
            DocSignal(
                polarity=signal.polarity,
                source=SOURCE_LABELS[signal.source],
                meaning=signal.meaning,
            )
            for signal in entry.signals.items
        ],
        signals_note=(
            "Listing text was flagged as an attempt to steer the model: nothing read from it "
            "is shown."
            if entry.signals.remarks_withheld
            else "Signals read from the listing; the listing's own words are not shown."
        ),
        narrative_status=narrative.status,
        narrative_note=narrative.note,
        summary=narrative.summary or "",
        risks=[DocRisk(text=risk.text, basis=", ".join(risk.basis)) for risk in narrative.risks],
        checks=list(narrative.checks_before_offer),
        assumptions_note=ASSUMPTIONS_NOTE[result.assumptions.status],
        footer=FOOTER,
    )

"""Plain-text lines for `feasibility brief show`. Figures go through `llm/figures.py`, the same
formatters the narrative was checked against."""

from decimal import Decimal

from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.llm import figures


def _money(value: str | None) -> str:
    return "none" if value is None else figures.money(Decimal(value))


def _candidate_lines(entry: BriefCandidate) -> list[str]:
    f = entry.figures
    lines = [
        f"#{entry.rank}  {entry.street}, {entry.zip5 or '?'}  score {entry.score}  "
        f"list price {_money(entry.list_price)}  (candidate {entry.candidate_id})",
        f"    ARV {_money(f.arv)}  total cost {_money(f.total_cost)}  profit {_money(f.profit)}  "
        f"margin {figures.percent(Decimal(f.margin))} "
        f"(target {figures.percent(Decimal(f.target_margin))})",
        f"    max offer {_money(f.max_offer)}  headroom {_money(f.headroom_vs_offer)}  "
        f"verdict {', '.join(entry.verdict)}",
        f"    comps: {figures.comps(entry.comps.count_used)}, "
        f"sale prices {_money(entry.comps.price_low)} to {_money(entry.comps.price_high)}, "
        f"estimate from {entry.comps.estimate_fetched_on}"
        f"{' (stale)' if entry.comps.estimate_stale else ''}",
    ]
    for flag in entry.flags:
        lines.append(f"    flag {flag.code}: {flag.meaning}")
    lines.append(f"    signals ({entry.signals.status}):")
    for signal in entry.signals.items:
        lines.append(f"      {signal.polarity:<11} {signal.code}: {signal.meaning}")
    narrative = entry.narrative
    lines.append(f"    narrative ({narrative.status}): {narrative.note}")
    if narrative.summary is not None:
        lines.append(f"      {narrative.summary}")
        lines.extend(f"      risk [{', '.join(r.basis)}]: {r.text}" for r in narrative.risks)
        lines.extend(
            f"      check before an offer: {text}" for text in narrative.checks_before_offer
        )
    return lines


def lines(brief: Brief, digest: str) -> list[str]:
    out = [
        f"brief for run {brief.run_id}: {brief.market} {brief.as_of}, {brief.completeness}"
        f"{' (' + brief.notice + ')' if brief.notice else ''}, data {brief.data_mode}",
        f"{brief.shown} shown of {brief.ranked} ranked; not shown: "
        f"{brief.not_shown.no_arv} with no value estimate yet, "
        f"{brief.not_shown.unsizable} unsizable, {brief.not_shown.over_the_cap} over the cap, "
        f"{brief.not_shown.no_pro_forma} with no pro-forma",
        f"content sha256 {digest}",
        "",
    ]
    for entry in brief.candidates:
        out.extend(_candidate_lines(entry))
        out.append("")
    out.append(brief.footer)
    return out

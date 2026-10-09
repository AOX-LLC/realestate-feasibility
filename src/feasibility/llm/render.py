"""Plain-text lines for the command line: a candidate's stored signals and narrative, and the
cost of a run or a month. Pure functions over stored results; nothing here reads the database.

As in the API, text comes only from an accepted narrative. A rejected one shows its reason and
the kinds of rule it broke."""

from decimal import Decimal

from feasibility.llm.narrative import NarrativeResult
from feasibility.llm.results import SignalsResult
from feasibility.llm.store import CostBlock, CostLine, MonthSpend


def signals_lines(result: SignalsResult | None) -> list[str]:
    if result is None:
        return ["signals: none stored for this candidate in this run"]
    head = f"signals: {result.status}" + (f" ({result.reason})" if result.reason else "")
    lines = [head]
    if result.remarks is not None:
        remarks = result.remarks
        rules = ", ".join(remarks.suspicious_rules) or "none"
        lines.append(
            f"  remarks: {remarks.char_count} characters, {remarks.redaction_count} redactions, "
            f"suspicious: {'yes' if remarks.suspicious else 'no'} (rules: {rules})"
        )
    for signal in result.signals:
        evidence = (
            f'"{signal.quote}"'
            if signal.source == "remarks"
            else f"{signal.field} = {signal.field_value}"
        )
        lines.append(f"  {signal.polarity:<11} {signal.code:<34} {signal.source:<7} {evidence}")
    if not result.signals:
        lines.append("  no signals")
    for claim in result.dropped:
        lines.append(f"  dropped claim: {claim.code} ({claim.reason})")
    if result.model_flagged_injection:
        lines.append("  the model flagged instructions in the remarks")
    if result.extraction is not None:
        extraction = result.extraction
        source = "reused" if extraction.reused else f"call {extraction.llm_call_id}"
        lines.append(
            f"  extraction: {extraction.prompt_id} v{extraction.prompt_version} on tier "
            f"{extraction.tier} ({source})"
        )
    return lines


def narrative_lines(result: NarrativeResult | None) -> list[str]:
    if result is None:
        return ["narrative: none stored for this candidate in this run"]
    head = f"narrative: {result.status}" + (f" ({result.reason})" if result.reason else "")
    lines = [head]
    if result.status == "accepted":
        lines.append(f"  {result.summary}")
        for risk in result.risks:
            lines.append(f"  risk [{', '.join(risk.basis)}]: {risk.text}")
        for check in result.checks_before_offer:
            lines.append(f"  check before an offer: {check}")
        if result.figures_quoted:
            quoted = ", ".join(f"{figure.key} {figure.text}" for figure in result.figures_quoted)
            lines.append(f"  figures quoted: {quoted}")
    if result.check is not None:
        kinds = ", ".join(violation.kind for violation in result.check.violations)
        lines.append(
            f"  check: {'passed' if result.check.passed else 'failed'} after "
            f"{result.check.attempts} attempt(s)" + (f"; broke: {kinds}" if kinds else "")
        )
    if result.model is not None:
        model = result.model
        source = "reused" if model.reused else "calls " + ", ".join(map(str, model.llm_call_ids))
        lines.append(
            f"  model: {model.prompt_id} v{model.prompt_version} on tier {model.tier} ({source})"
        )
    return lines


def _line(label: str, line: CostLine) -> str:
    return (
        f"  {label:<24} {line.calls:>4} calls  {line.refused:>2} refused  "
        f"{line.input_tokens:>8} in  {line.output_tokens:>7} out  "
        f"${line.cost_usd}  (+${line.reserved_unknown_usd} reserved, cost unknown)"
    )


def cost_lines(run_id: int, cost: CostBlock, budget: Decimal) -> list[str]:
    spent = cost.total.cost_usd + cost.total.reserved_unknown_usd
    lines = [
        f"run {run_id} model calls: modes {', '.join(cost.modes) or 'none'}",
        _line("total", cost.total),
        f"  spent as the cap counts it ${spent} of a ${budget} run budget",
    ]
    if cost.by_stage:
        lines.append("by stage:")
        lines.extend(_line(line.key or "(none)", line) for line in cost.by_stage)
    if cost.by_model:
        lines.append("by model:")
        lines.extend(_line(line.key or "(unknown: raised)", line) for line in cost.by_model)
    return lines


def month_lines(month: str, spend: MonthSpend, budget: Decimal) -> list[str]:
    spent = spend.cost_usd + spend.reserved_unknown_usd
    return [
        f"billable calls in {month} (UTC): {spend.calls} sent, {spend.refused} refused",
        f"  cost ${spend.cost_usd} (+${spend.reserved_unknown_usd} reserved, cost unknown)",
        f"  spent as the cap counts it ${spend.cost_usd + spend.reserved_unknown_usd} "
        f"of a ${budget} monthly budget; ${max(Decimal(0), budget - spent)} left",
    ]

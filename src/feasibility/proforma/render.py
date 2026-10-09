"""How stored pro-formas read on a terminal: a table for a run, labelled sections for one.

Pure formatting over the stored rows and JSON; nothing here reads the database. Money has
thousands separators and cents, ratios are percents with one decimal, a null is `none`.
"""

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from feasibility.proforma.model import ProformaResult
from feasibility.proforma.store import StoredProforma

NONE = "none"
# The result's fractions (0.1964 is 19.6%); every other key holds a quantity or an amount.
RATIO_KEYS = frozenset({"margin", "roi", "annualized_return", "target_margin"})
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
LIST_HEADER = ("rank", "address", "status", "arv", "total_cost", "profit", "margin", "max_offer")


def money(value: Decimal | str | None) -> str:
    return NONE if value is None else f"{Decimal(value):,.2f}"


def percent(fraction: Decimal | str | None) -> str:
    return NONE if fraction is None else f"{Decimal(fraction) * 100:.1f}%"


def list_lines(items: Sequence[StoredProforma]) -> list[str]:
    """A header and one tab-separated line per pro-forma: rank, address, status (or the reason
    it is not computed), ARV, total cost, profit, margin and the most that earns the target."""
    lines = ["\t".join(LIST_HEADER)]
    for item in items:
        state = item.status if item.status == "computed" else f"{item.status}: {item.reason}"
        lines.append(
            "\t".join(
                [
                    str(item.rank),
                    item.street,
                    state,
                    money(item.arv),
                    money(item.total_cost),
                    money(item.profit),
                    percent(item.margin),
                    money(item.max_offer),
                ]
            )
        )
    return lines


def _grouped(number: str) -> str:
    whole, _, fraction = number.partition(".")
    text = f"{int(whole):,}"
    if whole.startswith("-") and int(whole) == 0:
        text = "-" + text
    return f"{text}.{fraction}" if fraction else text


def _scalar(key: str, value: Any) -> str:
    if value is None:
        return NONE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key in RATIO_KEYS:
        return percent(value)
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        return _grouped(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _row(row: Mapping[str, Any]) -> str:
    return "  ".join(f"{key}={_value(key, value)}" for key, value in row.items())


def _value(key: str, value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_scalar(key, inner) for inner in value) or NONE
    return _scalar(key, value)


def _lines(key: str, value: Any, indent: int) -> list[str]:
    pad = "  " * indent
    if isinstance(value, Mapping):
        lines = [f"{pad}{key}:"]
        for name, inner in value.items():
            lines.extend(_lines(name, inner, indent + 1))
        return lines
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return [f"{pad}{key}:", *(f"{pad}  - {_row(row)}" for row in value)]
    if isinstance(value, list):
        return [f"{pad}{key}: {_value(key, value)}"]
    return [f"{pad}{key}: {_scalar(key, value)}"]


def _grid_blocks(sensitivity: Mapping[str, Any]) -> list[str]:
    """The sensitivity cells as one block per hold: rows are ARV changes, columns are hard-cost
    changes, each cell the profit and, in brackets, the margin."""
    axes = sensitivity["axes"]
    cells = {
        (c["arv_delta_pct"], c["hard_cost_delta_pct"], c["hold_months"]): c
        for c in sensitivity["cells"]
    }
    lines = []
    for hold in axes["hold_months"]:
        lines.append(
            f"  hold {hold} months (profit, margin; rows ARV change %, columns hard cost %):"
        )
        lines.append("    " + "\t".join(["arv\\cost", *axes["hard_cost_delta_pct"]]))
        for arv in axes["arv_delta_pct"]:
            row = [arv]
            for cost in axes["hard_cost_delta_pct"]:
                cell = cells[(arv, cost, hold)]
                row.append(f"{Decimal(cell['profit']):,.0f} ({percent(cell['margin'])})")
            lines.append("    " + "\t".join(row))
    return lines


def show_lines(item: StoredProforma, *, sensitivity: bool) -> list[str]:
    """The pro-forma as labelled sections in the order the result model declares. The sensitivity
    grid is long, so it is printed only when asked for."""
    if item.result is None:
        raise ValueError("show_lines needs the stored result")
    # JSONB does not keep key order, so the result is read back through its model: the sections
    # and their lines come out in the order the model declares them.
    result = ProformaResult.model_validate(item.result).model_dump(mode="json")
    lines = [
        f"candidate {item.candidate_id}, rank {item.rank}: {item.street}",
        f"offer price: {money(item.offer_price)}",
    ]
    for key, value in result.items():
        if key == "sensitivity" and isinstance(value, Mapping):
            lines.append("sensitivity:")
            lines.append(f"  axes: {_row(value['axes'])}")
            if sensitivity:
                lines.extend(_grid_blocks(value))
            else:
                lines.append(f"  {len(value['cells'])} cells; --sensitivity prints them")
        else:
            lines.extend(_lines(key, value, 0))
    return lines

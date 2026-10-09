"""Signals read from structured fields, computed in code in both data modes.

RentCast has no remarks, so in live mode these three are the only signals there are; in mock mode
they sit beside the model's remarks signals. Nothing here calls a model, and the evidence for each
is a field name and a value this code wrote.

`relisted` is a risk: a property that came back to market may have fallen out of a deal, and the
buyer should find out why. A price cut and a long stay are leverage, so both are opportunities.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from feasibility.llm.catalogue import Polarity

FieldSignalCode = Literal["price_reduced", "relisted", "long_on_market"]


# What each field signal means, written here, for the narrative's facts sheet. No digits.
FIELD_SIGNAL_MEANINGS: dict[str, str] = {
    "price_reduced": "The asking price was cut since the previous run.",
    "relisted": "The property came back on the market after being off it.",
    "long_on_market": "The property has been listed longer than the market's threshold.",
}


@dataclass(frozen=True)
class FieldSignalInput:
    """What the three rules read for the primary listing of one candidate in one run.

    `change_kind`, `price` and `prev_price` are the run's diff row for the listing (the previous
    price is the previous fresh run's)."""

    change_kind: str
    price: Decimal | None
    prev_price: Decimal | None
    listed_date: date | None
    as_of: date


@dataclass(frozen=True)
class FieldSignal:
    code: FieldSignalCode
    polarity: Polarity
    field: str
    field_value: str


def _price_reduced(inputs: FieldSignalInput) -> FieldSignal | None:
    if inputs.change_kind != "price_changed":
        return None
    if inputs.price is None or inputs.prev_price is None or inputs.price >= inputs.prev_price:
        return None
    return FieldSignal(
        "price_reduced", "opportunity", "price", f"{inputs.prev_price} to {inputs.price}"
    )


def _relisted(inputs: FieldSignalInput) -> FieldSignal | None:
    if inputs.change_kind != "relisted":
        return None
    return FieldSignal("relisted", "risk", "change_kind", "relisted")


def _long_on_market(inputs: FieldSignalInput, threshold_days: int) -> FieldSignal | None:
    if inputs.listed_date is None:
        return None
    if (inputs.as_of - inputs.listed_date).days < threshold_days:
        return None
    return FieldSignal(
        "long_on_market",
        "opportunity",
        "listed_date",
        f"{inputs.listed_date.isoformat()} as of {inputs.as_of.isoformat()}",
    )


def field_signals(inputs: FieldSignalInput, long_on_market_days: int) -> list[FieldSignal]:
    """The field signals that hold, in a fixed order: price_reduced, relisted, long_on_market."""
    found = (
        _price_reduced(inputs),
        _relisted(inputs),
        _long_on_market(inputs, long_on_market_days),
    )
    return [signal for signal in found if signal is not None]

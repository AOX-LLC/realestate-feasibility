"""Field signals: computed in code from the run's diff and the listing's dates, in both modes."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from feasibility.llm.field_signals import FieldSignalInput, field_signals

AS_OF = date(2026, 10, 1)
THRESHOLD = 60


def signal_input(**changes: object) -> FieldSignalInput:
    fields: dict[str, object] = {
        "change_kind": "unchanged",
        "price": Decimal("500000.00"),
        "prev_price": Decimal("500000.00"),
        "listed_date": AS_OF - timedelta(days=10),
        "as_of": AS_OF,
    }
    fields.update(changes)
    return FieldSignalInput(**fields)  # type: ignore[arg-type]


def codes(inputs: FieldSignalInput) -> list[str]:
    return [signal.code for signal in field_signals(inputs, THRESHOLD)]


def test_a_lower_price_than_the_previous_run_is_price_reduced() -> None:
    inputs = signal_input(change_kind="price_changed", price=Decimal("479000.00"))

    [signal] = field_signals(inputs, THRESHOLD)

    assert signal.code == "price_reduced"
    assert signal.polarity == "opportunity"
    assert signal.field == "price"
    assert signal.field_value == "500000.00 to 479000.00"


def test_a_higher_price_is_not_price_reduced() -> None:
    assert codes(signal_input(change_kind="price_changed", price=Decimal("519000.00"))) == []


def test_an_unchanged_listing_is_not_price_reduced_even_with_two_prices() -> None:
    assert codes(signal_input(price=Decimal("1.00"))) == []


@pytest.mark.parametrize("price", [None, Decimal("479000.00")])
def test_price_reduced_needs_both_prices(price: Decimal | None) -> None:
    inputs = signal_input(change_kind="price_changed", price=price, prev_price=None)

    assert codes(inputs) == []


def test_a_relisted_listing_is_relisted() -> None:
    [signal] = field_signals(signal_input(change_kind="relisted"), THRESHOLD)

    assert (signal.code, signal.field, signal.field_value) == (
        "relisted",
        "change_kind",
        "relisted",
    )
    assert signal.polarity == "risk"


@pytest.mark.parametrize(
    ("days", "expected"), [(59, []), (60, ["long_on_market"]), (61, ["long_on_market"])]
)
def test_long_on_market_starts_at_the_threshold(days: int, expected: list[str]) -> None:
    assert codes(signal_input(listed_date=AS_OF - timedelta(days=days))) == expected


def test_long_on_market_reports_the_dates_it_used() -> None:
    [signal] = field_signals(signal_input(listed_date=date(2026, 7, 1)), THRESHOLD)

    assert signal.field == "listed_date"
    assert signal.field_value == "2026-07-01 as of 2026-10-01"
    assert signal.polarity == "opportunity"


def test_a_listing_with_no_listed_date_is_never_long_on_market() -> None:
    assert codes(signal_input(listed_date=None)) == []


def test_a_listed_date_after_the_as_of_date_is_never_long_on_market() -> None:
    assert codes(signal_input(listed_date=AS_OF + timedelta(days=5))) == []


def test_signals_come_in_a_fixed_order() -> None:
    inputs = signal_input(
        change_kind="price_changed",
        price=Decimal("479000.00"),
        listed_date=AS_OF - timedelta(days=90),
    )

    assert codes(inputs) == ["price_reduced", "long_on_market"]


def test_the_threshold_is_the_callers_not_a_constant() -> None:
    inputs = signal_input(listed_date=AS_OF - timedelta(days=30))

    assert [s.code for s in field_signals(inputs, 30)] == ["long_on_market"]
    assert [s.code for s in field_signals(inputs, 31)] == []

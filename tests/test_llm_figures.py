"""Figure strings: money to the cent, ratios as exact percents, sizes and counts."""

from decimal import Decimal

import pytest

from feasibility.llm import figures


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1420799.85", "$1,420,799.85"),
        ("107560.14", "$107,560.14"),
        ("-512840.11", "-$512,840.11"),
        ("0", "$0.00"),
        ("-0.004", "$0.00"),  # never "-$0.00"
        ("5", "$5.00"),
        ("1234", "$1,234.00"),
        ("0.01", "$0.01"),
        ("1000000", "$1,000,000.00"),
        ("2.345", "$2.35"),  # half away from zero, as every pro-forma figure rounds
        ("-2.345", "-$2.35"),
    ],
)
def test_money(value: str, text: str) -> None:
    assert figures.money(Decimal(value)) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("0.0757", "7.57%"),
        ("0.15", "15.00%"),
        ("0.1599", "15.99%"),
        ("-0.7559", "-75.59%"),
        ("1.0610", "106.10%"),
        ("0", "0.00%"),
        ("-0.00001", "0.00%"),
        ("0.0001", "0.01%"),
        ("2.2649", "226.49%"),
    ],
)
def test_ratio_is_a_percent_with_two_decimals(value: str, text: str) -> None:
    assert figures.percent(Decimal(value)) == text


def test_a_four_decimal_ratio_is_shown_exactly() -> None:
    """Four decimals as a fraction are two decimals as a percent, so nothing is rounded."""
    for ten_thousandths in range(-30000, 30001, 137):
        ratio = Decimal(ten_thousandths) / Decimal(10000)
        shown = figures.percent(ratio)
        assert Decimal(shown.rstrip("%")) == ratio * 100


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("3168", "3,168 sq ft"),
        ("6400.00", "6,400 sq ft"),
        ("999", "999 sq ft"),
        ("10134", "10,134 sq ft"),
    ],
)
def test_area(value: str, text: str) -> None:
    assert figures.area(Decimal(value)) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("9", "9 months"),
        ("12", "12 months"),
        ("1", "1 month"),
        ("9.0", "9 months"),
        ("7.5", "7.5 months"),
    ],
)
def test_months(value: str, text: str) -> None:
    assert figures.months(Decimal(value)) == text


@pytest.mark.parametrize(("value", "text"), [(5, "5 comps"), (1, "1 comp"), (12, "12 comps")])
def test_comp_count(value: int, text: str) -> None:
    assert figures.comps(value) == text


def test_every_figure_has_a_digit_in_it() -> None:
    for text in (
        figures.money(Decimal("1")),
        figures.percent(Decimal("0")),
        figures.area(Decimal("1")),
        figures.months(Decimal("1")),
        figures.comps(1),
    ):
        assert any(character.isdigit() for character in text)

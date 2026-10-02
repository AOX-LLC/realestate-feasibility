from decimal import Decimal

import pytest

from feasibility.proforma.money import (
    CENT,
    round_money,
    round_money_down,
    round_ratio,
    round_sqft,
)


def test_cent_is_one_hundredth() -> None:
    assert Decimal("0.01") == CENT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2.675", "2.68"),  # half away from zero, not banker's rounding
        ("2.665", "2.67"),
        ("-2.675", "-2.68"),
        ("0.004", "0.00"),
        ("1420799.8496", "1420799.85"),
    ],
)
def test_round_money_rounds_half_away_from_zero(value: str, expected: str) -> None:
    assert round_money(Decimal(value)) == Decimal(expected)
    assert str(round_money(Decimal(value))) == expected


def test_round_money_down_truncates_toward_zero() -> None:
    assert str(round_money_down(Decimal("324271.5099"))) == "324271.50"
    assert str(round_money_down(Decimal("0.019"))) == "0.01"


def test_round_ratio_keeps_four_places() -> None:
    assert str(round_ratio(Decimal("0.07565"))) == "0.0757"
    assert str(round_ratio(Decimal("-0.75594"))) == "-0.7559"


def test_round_sqft_keeps_whole_feet() -> None:
    assert str(round_sqft(Decimal("3167.5"))) == "3168"
    assert str(round_sqft(Decimal("3167.49"))) == "3167"


def test_exact_halves_round_up_not_to_even() -> None:
    assert str(round_sqft(Decimal("3166.5"))) == "3167"
    assert str(round_sqft(Decimal("3167.5"))) == "3168"

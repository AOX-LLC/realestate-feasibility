"""The one place rounding modes live: every rounded figure in the pro-forma comes from here."""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
RATIO_STEP = Decimal("0.0001")
WHOLE = Decimal(1)


def round_money(value: Decimal) -> Decimal:
    """Round to cents, half away from zero."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def round_money_down(value: Decimal) -> Decimal:
    """Round to cents toward zero (the maximum offer never rounds up)."""
    return value.quantize(CENT, rounding=ROUND_DOWN)


def round_ratio(value: Decimal) -> Decimal:
    """Round a ratio to four places, half away from zero."""
    return value.quantize(RATIO_STEP, rounding=ROUND_HALF_UP)


def round_sqft(value: Decimal) -> Decimal:
    """Round a size to whole square feet, half away from zero."""
    return value.quantize(WHOLE, rounding=ROUND_HALF_UP)

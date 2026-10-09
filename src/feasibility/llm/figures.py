"""Figure strings: the only way a number may appear in a narrative.

Each formatter turns a stored value into the exact text the narrative must repeat. Money is shown
to the cent and a ratio as a percent with two decimals, which loses nothing for the pro-forma's
four-decimal ratios. The checker (`narrative.check_narrative`) rejects every digit that is not
inside one of these strings.
"""

from decimal import ROUND_HALF_UP, Decimal

from feasibility.proforma.money import round_money

_PERCENT_STEP = Decimal("0.01")
_HUNDRED = Decimal(100)


def money(value: Decimal) -> str:
    """`$1,420,799.85`, or `-$512,840.11`; zero is `$0.00`, never `-$0.00`."""
    cents = round_money(value)
    if cents == 0:
        return "$0.00"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents):,.2f}"


def percent(ratio: Decimal) -> str:
    """A fraction as a percent with two decimals: `0.0757` is `7.57%`."""
    shown = (ratio * _HUNDRED).quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)
    return "0.00%" if shown == 0 else f"{shown:.2f}%"


def _plain(value: Decimal) -> str:
    """A quantity with its thousands separators and no trailing zeros: `6,400`, `7.5`."""
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value.normalize():,f}"


def area(value: Decimal) -> str:
    return f"{_plain(value)} sq ft"


def months(value: Decimal) -> str:
    text = _plain(value)
    return f"{text} {'month' if text == '1' else 'months'}"


def comps(count: int) -> str:
    return f"{count} {'comp' if count == 1 else 'comps'}"

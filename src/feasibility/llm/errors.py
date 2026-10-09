"""Errors this package raises itself. The model library's own errors pass through unchanged."""

from decimal import Decimal


class LlmNotConfiguredError(RuntimeError):
    """A model client was asked for, but live data has no model configured."""


class LlmBudgetError(RuntimeError):
    """A call would take spend past a cap, so it was not sent.

    `scope` is the cap that refused it: "run", "monthly" or "session".
    """

    def __init__(self, scope: str, *, spent: Decimal, reservation: Decimal, cap: Decimal) -> None:
        super().__init__(
            f"{scope} model budget: ${spent} spent and ${reservation} reserved "
            f"would pass the ${cap} cap"
        )
        self.scope = scope
        self.spent = spent
        self.reservation = reservation
        self.cap = cap

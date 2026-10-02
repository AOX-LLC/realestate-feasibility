"""Errors the sourcing run raises on purpose. All are permanent: retrying the same input
cannot succeed."""


class SourcingError(RuntimeError):
    """Base class for sourcing failures that a retry cannot fix."""


class NoSnapshotForDateError(SourcingError):
    """Mock mode was asked for a day the committed snapshot does not hold."""


class RunOutOfOrderError(SourcingError):
    """A run was requested for a date earlier than the latest completed run."""


class LiveDateError(SourcingError):
    """Live mode only sources for today in the market's time zone."""

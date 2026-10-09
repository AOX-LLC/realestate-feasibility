"""Which day a run is for. Apart from the run itself so that a module that only checks a date
(the trigger route) does not import the code that syncs, spends and calls a model."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from feasibility.config import Settings
from feasibility.markets.schema import MarketPack
from feasibility.snapshot.days import snapshot_day, snapshot_days
from feasibility.sourcing.errors import LiveDateError, NoSnapshotForDateError


def resolve_run_date(
    settings: Settings, pack: MarketPack, as_of: date | None
) -> tuple[date, str | None]:
    """The date to source for and, in mock mode, the snapshot overlay that holds its feed.

    Live mode sources today (in the market's time zone) only. Mock mode needs a date the
    snapshot holds; the next date is never inferred.
    """
    market = pack.market.id
    if settings.is_live:
        today = datetime.now(ZoneInfo(pack.market.timezone)).date()
        if as_of is not None and as_of != today:
            raise LiveDateError(f"live mode sources for today only ({today}), not {as_of}")
        return today, None
    if as_of is None:
        available = ", ".join(day.as_of.isoformat() for day in snapshot_days(settings, market))
        raise NoSnapshotForDateError(
            f"mock mode needs an explicit date; available dates: {available or 'none'}"
        )
    return as_of, snapshot_day(settings, market, as_of)

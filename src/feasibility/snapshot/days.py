"""The snapshot days: which dates mock mode can source, and which overlay directory
holds each day's listing feed (None means the base `rentcast/` directory)."""

import json
from dataclasses import dataclass
from datetime import date

from feasibility.config import Settings
from feasibility.sourcing.errors import NoSnapshotForDateError

DAYS_FILE = "days.json"


@dataclass(frozen=True)
class SnapshotDay:
    as_of: date
    overlay: str | None


def snapshot_days(settings: Settings, market: str) -> list[SnapshotDay]:
    """Every snapshot day for the market, oldest first. Empty when the snapshot has none."""
    days_file = settings.snapshot_dir / DAYS_FILE
    if not days_file.is_file():
        return []
    recorded = json.loads(days_file.read_text(encoding="utf-8"))
    if recorded["market"] != market:
        return []
    days = [
        SnapshotDay(date.fromisoformat(day["as_of"]), day["overlay"]) for day in recorded["days"]
    ]
    return sorted(days, key=lambda day: day.as_of)


def snapshot_day(settings: Settings, market: str, as_of: date) -> str | None:
    """The overlay directory name for `as_of`, or None for the base feed.

    Raises NoSnapshotForDateError, listing the available dates, when there is no such day.
    """
    days = snapshot_days(settings, market)
    for day in days:
        if day.as_of == as_of:
            return day.overlay
    available = ", ".join(day.as_of.isoformat() for day in days) or "none"
    raise NoSnapshotForDateError(
        f"the snapshot has no day {as_of.isoformat()} for {market}; available dates: {available}"
    )

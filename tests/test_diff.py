from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from feasibility.sourcing.diff import (
    AbsentListing,
    FeedListing,
    PreviousListing,
    absence_kind,
    classify,
)

AS_OF = date(2026, 10, 2)
WINDOW_START = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
WINDOW_DAYS = 2
SEEN_TODAY = WINDOW_START + timedelta(hours=6)
SEEN_EARLIER = WINDOW_START - timedelta(days=1)


def _feed(listing_id: int, price: int | None, first_seen: datetime = SEEN_TODAY) -> FeedListing:
    return FeedListing(listing_id, None if price is None else Decimal(price), first_seen)


def _before(listing_id: int, price: int | None) -> PreviousListing:
    return PreviousListing(listing_id, None if price is None else Decimal(price))


def _absent(
    listing_id: int, status: str | None = "Active", listed: date | None = date(2026, 10, 1)
) -> AbsentListing:
    return AbsentListing(listing_id, status, listed)


def _classify(
    feed: list[FeedListing],
    previous: dict[int, PreviousListing] | None,
    *,
    absent: dict[int, AbsentListing] | None = None,
    sync_status: str = "fresh",
    returning: set[int] | None = None,
) -> dict[int, tuple[str, Decimal | None, Decimal | None]]:
    result = classify(
        feed,
        previous,
        absent or {},
        window_start=WINDOW_START,
        as_of=AS_OF,
        feed_window_days=WINDOW_DAYS,
        sync_status=sync_status,  # type: ignore[arg-type]
        returning=returning or set(),
    )
    return {c.listing_id: (c.kind, c.price, c.prev_price) for c in result.changes}


def test_with_no_previous_run_everything_is_new() -> None:
    changes = _classify([_feed(1, 100), _feed(2, 200, SEEN_EARLIER)], None)
    assert [kind for kind, _, _ in changes.values()] == ["new", "new"]


@pytest.mark.parametrize(
    ("price", "kind", "prev_price"),
    [
        (100, "unchanged", Decimal(100)),
        (90, "price_changed", Decimal(100)),
        (110, "price_changed", Decimal(100)),
    ],
)
def test_a_listing_in_both_runs_is_unchanged_or_repriced(
    price: int, kind: str, prev_price: Decimal
) -> None:
    changes = _classify([_feed(1, price)], {1: _before(1, 100)})
    assert changes[1] == (kind, Decimal(price), prev_price)


def test_a_new_listing_id_for_a_known_property_is_relisted() -> None:
    changes = _classify(
        [_feed(9, 100)], {1: _before(1, 100)}, returning={9}, absent={1: _absent(1)}
    )
    assert changes[9][0] == "relisted"


def test_the_same_id_returning_after_a_gap_is_relisted() -> None:
    changes = _classify([_feed(1, 100, SEEN_EARLIER)], {2: _before(2, 50)}, absent={2: _absent(2)})
    assert changes[1][0] == "relisted"


def test_a_listing_first_seen_today_is_new() -> None:
    changes = _classify([_feed(5, 100)], {2: _before(2, 50)}, absent={2: _absent(2)})
    assert changes[5][0] == "new"


@pytest.mark.parametrize(
    ("absent", "kind"),
    [
        # Young enough to still be in the feed: gone.
        (_absent(1, listed=date(2026, 10, 1)), "gone"),
        (_absent(1, listed=date(2026, 9, 30)), "gone"),
        # Left the window by age: nothing is known about a sale.
        (_absent(1, listed=date(2026, 9, 29)), "aged_out"),
        # Status says it is off the market, whatever its age.
        (_absent(1, status="Inactive", listed=date(2026, 9, 1)), "gone"),
        # A missing date is not evidence; a status flip is.
        (_absent(1, listed=None), "aged_out"),
        (_absent(1, status="Inactive", listed=None), "gone"),
    ],
)
def test_a_listing_the_feed_lost_is_gone_or_aged_out(absent: AbsentListing, kind: str) -> None:
    changes = _classify([], {1: _before(1, 100)}, absent={1: absent})
    assert changes[1] == (kind, Decimal(100), Decimal(100))
    assert absence_kind(absent, AS_OF, WINDOW_DAYS) == kind


@pytest.mark.parametrize("sync_status", ["stale", "skipped"])
def test_absence_proves_nothing_unless_the_sync_was_fresh(sync_status: str) -> None:
    result = classify(
        [],
        {1: _before(1, 100), 2: _before(2, 200)},
        {1: _absent(1), 2: _absent(2)},
        window_start=WINDOW_START,
        as_of=AS_OF,
        feed_window_days=WINDOW_DAYS,
        sync_status=sync_status,  # type: ignore[arg-type]
        returning=set(),
    )
    assert result.changes == []
    assert result.unknown_absent == 2


def test_listings_in_the_feed_are_not_counted_as_lost() -> None:
    changes = _classify(
        [_feed(1, 100)], {1: _before(1, 100), 2: _before(2, 50)}, absent={2: _absent(2)}
    )
    assert {i: kind for i, (kind, _, _) in changes.items()} == {1: "unchanged", 2: "gone"}

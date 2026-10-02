"""The daily diff, as a pure function.

The RentCast feed is a window (`days_old`), not the inventory: a listing older than the
window stops appearing though it may still be for sale. So absence from today's feed is
`gone` only when the listing was young enough to still be in the window (or its status
says it is off the market); otherwise it is `aged_out` and nothing is known about a sale.
A listing with no listed date is `aged_out`: a missing date proves nothing, a status flip does.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

ChangeKind = Literal["new", "relisted", "price_changed", "unchanged", "gone", "aged_out"]
SyncStatus = Literal["fresh", "stale", "skipped", "pending"]


@dataclass(frozen=True, slots=True)
class FeedListing:
    """A listing seen in today's window."""

    listing_id: int
    price: Decimal | None
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class PreviousListing:
    """A listing the previous fresh run saw and still had in its feed."""

    listing_id: int
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class AbsentListing:
    """What the listing row says now about a listing missing from today's feed."""

    listing_id: int
    status: str | None
    listed_date: date | None


@dataclass(frozen=True, slots=True)
class ListingChange:
    listing_id: int
    kind: ChangeKind
    price: Decimal | None
    prev_price: Decimal | None


@dataclass(frozen=True, slots=True)
class Classification:
    changes: list[ListingChange]
    # Listings missing from the feed while the sync was not fresh: absence proves nothing.
    unknown_absent: int


def absence_kind(
    absent: AbsentListing, as_of: date, feed_window_days: int
) -> Literal["gone", "aged_out"]:
    """`gone` when the listing should still be in the feed and is not."""
    if absent.status != "Active":
        return "gone"
    if absent.listed_date is None:
        return "aged_out"
    still_in_window = absent.listed_date >= as_of - timedelta(days=feed_window_days)
    return "gone" if still_in_window else "aged_out"


def classify(
    feed: list[FeedListing],
    previous: Mapping[int, PreviousListing] | None,
    absent: Mapping[int, AbsentListing],
    *,
    window_start: datetime,
    as_of: date,
    feed_window_days: int,
    sync_status: SyncStatus,
    returning: Collection[int],
) -> Classification:
    """Classify every listing in today's feed and every listing the feed lost.

    `previous` is None when there is no earlier completed run: everything is new.
    `absent` holds the current rows of the previous run's listings that are not in `feed`.
    `returning` holds the feed listings whose property already had a candidate on an
    earlier run date (a new listing id for a property seen before).
    """
    if previous is None:
        return Classification(
            [ListingChange(item.listing_id, "new", item.price, None) for item in feed], 0
        )

    changes = [_classify_feed_listing(item, previous, window_start, returning) for item in feed]

    feed_ids = {item.listing_id for item in feed}
    lost = sorted(listing_id for listing_id in previous if listing_id not in feed_ids)
    if sync_status != "fresh":
        return Classification(changes, unknown_absent=len(lost))

    for listing_id in lost:
        last_price = previous[listing_id].price
        kind = absence_kind(absent[listing_id], as_of, feed_window_days)
        changes.append(ListingChange(listing_id, kind, last_price, last_price))
    return Classification(changes, 0)


def _classify_feed_listing(
    item: FeedListing,
    previous: Mapping[int, PreviousListing],
    window_start: datetime,
    returning: Collection[int],
) -> ListingChange:
    before = previous.get(item.listing_id)
    if before is not None:
        kind: ChangeKind = "unchanged" if before.price == item.price else "price_changed"
        return ListingChange(item.listing_id, kind, item.price, before.price)

    came_back = item.first_seen_at < window_start or item.listing_id in returning
    return ListingChange(item.listing_id, "relisted" if came_back else "new", item.price, None)

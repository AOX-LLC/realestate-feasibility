"""What one sourcing run did, as stored in sourcing_run.counts and served by the API."""

from pydantic import BaseModel, ConfigDict


class FilteredByReason(BaseModel):
    model_config = ConfigDict(frozen=True)

    zip: int = 0
    property_type: int = 0
    price: int = 0


class RunCounts(BaseModel):
    """The new / relisted / price_changed / unchanged counters are per listing and include
    listings that failed the listing-level filters. matched / ambiguous / unmatched are per
    listing that was matched; the candidate counters are per property."""

    model_config = ConfigDict(frozen=True)

    listings_seen: int = 0
    new: int = 0
    relisted: int = 0
    price_changed: int = 0
    unchanged: int = 0
    gone: int = 0
    aged_out: int = 0
    unknown_absent: int = 0
    inactive_ignored: int = 0
    listing_filtered: int = 0
    listing_filtered_by_reason: FilteredByReason = FilteredByReason()
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    # matched / (matched + ambiguous + unmatched) as a 4-decimal string; None when nothing
    # was matched this run.
    match_rate: str | None = None
    candidates: int = 0
    ranked: int = 0
    filtered: int = 0
    unscored: int = 0

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
    # matched / (matched + ambiguous + unmatched) as a 4-decimal string; None when no listing
    # reached matching this run (so 0 of 5 matched is "0.0000", not None).
    match_rate: str | None = None
    candidates: int = 0
    ranked: int = 0
    filtered: int = 0
    unscored: int = 0
    # The value-estimate spend. Every targeted candidate is reused, called, deferred or
    # failed; a call RentCast answered with no estimate is also counted in no_estimate.
    # Zero on runs stored before the spend existed.
    estimates_targeted: int = 0
    estimates_reused: int = 0
    estimates_called: int = 0
    estimates_no_estimate: int = 0
    estimates_deferred: int = 0
    estimates_failed: int = 0
    # The pro-formas of the ranked candidates (stage 5). Zero on runs stored before they existed.
    proformas: int = 0
    proformas_computed: int = 0
    proformas_no_arv: int = 0
    proformas_unsizable: int = 0
    # The model stages (6 signals, 7 narratives). Zero on runs stored before they existed.
    # `signals_*` and `narratives_*` count the run's candidates by what was stored for them.
    signals_extracted: int = 0
    signals_fields_only: int = 0
    signals_failed: int = 0
    signals_deferred: int = 0
    signals_reused: int = 0
    signals_quotes_dropped: int = 0
    signals_suspicious: int = 0
    narratives_accepted: int = 0
    narratives_rejected: int = 0
    narratives_failed: int = 0
    narratives_deferred: int = 0
    narratives_not_eligible: int = 0
    narratives_reused: int = 0
    narratives_repaired: int = 0
    # The calls this attempt of the run made (a call a cap refused is not one) and what they
    # cost, as a string; None when it made none. A re-run that reuses everything makes none.
    llm_calls: int = 0
    llm_cost_usd: str | None = None

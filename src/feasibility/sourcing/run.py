"""One sourcing run: sync the feed, diff it against the previous run, match, filter, score,
rank and store the result, then price the top candidates and compute a pro-forma for each
ranked one.

A run is keyed by (market, as_of) and idempotent: running the same date again rewrites the
run's own rows and leaves candidates and matches unduplicated. Runs go forward in time only.
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import Connection, Engine

from feasibility.config import Settings
from feasibility.jobs.queue import ERROR_TEXT_LIMIT
from feasibility.listings import sync_listings
from feasibility.logging import redact
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import MarketPack, RentCastListings
from feasibility.proforma.run import ProformaCounts, run_proformas
from feasibility.snapshot.days import snapshot_day, snapshot_days
from feasibility.sources.mls.reso import remarks_source_for
from feasibility.sources.rentcast.client import BudgetExhaustedError, RentCastClient
from feasibility.sourcing import diff, estimates, store
from feasibility.sourcing.counts import FilteredByReason, RunCounts
from feasibility.sourcing.errors import LiveDateError, NoSnapshotForDateError, RunOutOfOrderError
from feasibility.sourcing.estimates import EstimateCounts
from feasibility.sourcing.filters import (
    ListingFacts,
    has_usable_values,
    listing_filter_reason,
    parcel_filter_reasons,
)
from feasibility.sourcing.keys import StreetKey, parse_listing_street, property_key
from feasibility.sourcing.matching import MatchResult, aggregate, load_parcel_index, match_listing
from feasibility.sourcing.scoring import RankKey, ScoreBreakdown, rank_order, score_candidate

log = logging.getLogger(__name__)

MOCK_SYNC_TIME = time(6, 0)
MATCH_RATE_STEP = Decimal("0.0001")
DEFAULT_FEED_WINDOW_DAYS = 1


@dataclass(frozen=True, slots=True)
class SourcingResult:
    run_id: int
    as_of: date
    sync_status: str
    counts: RunCounts


@dataclass(frozen=True, slots=True)
class PropertyRef:
    """Which property a listing is for, as a candidate key."""

    property_key: str
    # The key the property had while unmatched; a later match upgrades that candidate.
    address_key: str
    account_id: str | None
    gis_parcel_id: str | None
    zip5: str | None
    street_key: str


@dataclass(frozen=True, slots=True)
class ResolvedListing:
    row: store.ListingRow
    facts: ListingFacts
    filter_reason: str | None
    match: MatchResult | None = None
    ref: PropertyRef | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    status: Literal["ranked", "filtered", "unscored"]
    filter_reasons: tuple[str, ...] = ()
    unscored_reason: str | None = None
    breakdown: ScoreBreakdown | None = None


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


def run_sourcing(
    engine: Engine,
    settings: Settings,
    market: str,
    as_of: date | None,
    *,
    client: RentCastClient | None = None,
) -> SourcingResult:
    """Source one day. A failure in the sync or the build marks the run failed and propagates.
    A failure after the build (the estimate spend, the pro-formas) leaves the ranked run
    completed with its error recorded, and also propagates."""
    pack = get_pack(market)
    run_date, overlay = resolve_run_date(settings, pack, as_of)
    run_id = _start_run(engine, market, run_date)
    if client is not None:
        return _source(engine, settings, pack, run_id, run_date, client)
    # The response cache would answer a later snapshot day with the earlier day's body.
    own_client = RentCastClient.from_settings(
        engine, settings, use_cache=settings.is_live, snapshot_day=overlay
    )
    try:
        return _source(engine, settings, pack, run_id, run_date, own_client)
    finally:
        own_client.close()


def _source(
    engine: Engine,
    settings: Settings,
    pack: MarketPack,
    run_id: int,
    run_date: date,
    client: RentCastClient,
) -> SourcingResult:
    """The run's stages after it has started, in the order of docs/ARCHITECTURE.md."""
    try:
        sync_status = _sync(engine, settings, pack, run_id, run_date, client)
        with engine.begin() as connection:
            counts = _build_run(connection, pack, run_id, run_date, sync_status)
    except Exception as error:
        message = _error_message(error, settings)
        with engine.begin() as connection:
            # A failed run must not serve the previous attempt's rows.
            store.clear_run_rows(connection, run_id)
            store.fail_run(connection, run_id, message)
        raise
    # The ranking is stored and the run completed: a later stage that fails leaves both in
    # place, records its error on the run and propagates so the job retries.
    estimate_counts = _spend_estimates(engine, settings, pack, run_id, run_date, client)
    proforma_counts = _price_proformas(engine, settings, pack, run_id, run_date)
    late_counts = {**asdict(estimate_counts), **asdict(proforma_counts)}
    return SourcingResult(run_id, run_date, sync_status, counts.model_copy(update=late_counts))


def _spend_estimates(
    engine: Engine,
    settings: Settings,
    pack: MarketPack,
    run_id: int,
    run_date: date,
    client: RentCastClient,
) -> EstimateCounts:
    try:
        return estimates.spend_estimates(
            engine,
            client,
            pack.sourcing.estimates,
            run_id=run_id,
            as_of=run_date,
            billing_anchor_day=settings.rentcast_billing_anchor_day,
            secrets=settings.secret_values(),
        )
    except Exception as error:
        _record_stage_error(engine, settings, run_id, error)
        raise


def _price_proformas(
    engine: Engine, settings: Settings, pack: MarketPack, run_id: int, run_date: date
) -> ProformaCounts:
    """Stage 5, a pro-forma for every ranked candidate. A failure leaves the ranked run and
    its estimates in place, like a failed spend, and propagates so the job retries."""
    try:
        with engine.begin() as connection:
            return run_proformas(connection, pack, run_id, run_date)
    except Exception as error:
        _record_stage_error(engine, settings, run_id, error)
        raise


def _error_message(error: Exception, settings: Settings) -> str:
    """What a failure says on the run: redacted, and cut to the length the job queue keeps (a
    database error can carry the statement and its values)."""
    text = redact(_describe(error), settings.secret_values())
    return text[:ERROR_TEXT_LIMIT]


def _describe(error: Exception) -> str:
    """The error as one line. A validation error is summarised without the values that failed,
    which can be listing or comparable-sale data."""
    if isinstance(error, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors(include_input=False, include_url=False)
        )
        return f"ValidationError: {error.error_count()} problems in {error.title}: {problems}"
    return f"{type(error).__name__}: {error}"


def _record_stage_error(engine: Engine, settings: Settings, run_id: int, error: Exception) -> None:
    """Note on the completed run that a stage after the build failed (redacted)."""
    message = _error_message(error, settings)
    try:
        with engine.begin() as connection:
            store.set_run_error(connection, run_id, message)
    except Exception:
        # The stage's own failure must propagate, not this one.
        log.exception("could not record the error of run %s", run_id)


def _refuse_if_out_of_order(connection: Connection, market: str, as_of: date) -> None:
    latest = store.latest_run_as_of(connection, market)
    if latest is not None and as_of < latest:
        raise RunOutOfOrderError(
            f"cannot source {as_of}: a run for {latest} has already been started"
        )


def _start_run(engine: Engine, market: str, as_of: date) -> int:
    with engine.begin() as connection:
        store.lock_market_runs(connection, market)
        _refuse_if_out_of_order(connection, market, as_of)
        return store.start_run(connection, market, as_of)


def _sync(
    engine: Engine,
    settings: Settings,
    pack: MarketPack,
    run_id: int,
    as_of: date,
    client: RentCastClient,
) -> Literal["fresh", "stale", "skipped"]:
    observed_at = (
        None
        if settings.is_live
        else datetime.combine(as_of, MOCK_SYNC_TIME, tzinfo=ZoneInfo(pack.market.timezone))
    )
    sync_status: Literal["fresh", "stale", "skipped"]
    try:
        sync_status = sync_listings(
            engine, pack, client, remarks_source_for(settings, pack.market.id), observed_at
        )
    except BudgetExhaustedError:
        sync_status = "skipped"
    with engine.begin() as connection:
        store.set_sync_status(connection, run_id, sync_status)
    return sync_status


def _window(as_of: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(as_of, time.min, tzinfo=zone),
        datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=zone),
    )


def _feed_window_days(pack: MarketPack) -> int:
    days = [
        spec.days_old
        for spec in pack.sources.listings
        if isinstance(spec, RentCastListings) and spec.enabled
    ]
    return max(days, default=DEFAULT_FEED_WINDOW_DAYS)


def _facts(row: store.ListingRow) -> ListingFacts:
    return ListingFacts(row.zip5, row.property_type, row.price, row.lot_size_sqft, row.year_built)


def _property_ref(row: store.ListingRow, result: MatchResult) -> PropertyRef:
    parsed = parse_listing_street(row.address_line, row.unit)
    # An address with no street number cannot be parsed; its normalized text stands in.
    key, unit = parsed or (StreetKey("", "", result.street_key, result.street_key), None)
    matched = aggregate(result) if result.status == "matched" else None
    account_id = matched.account_id if matched else None
    gis_parcel_id = matched.gis_parcel_id if matched else None
    return PropertyRef(
        property_key=property_key(account_id, gis_parcel_id, result.method, row.zip5, key, unit),
        address_key=property_key(None, None, None, row.zip5, key, unit),
        account_id=account_id,
        gis_parcel_id=gis_parcel_id,
        zip5=row.zip5,
        street_key=result.street_key,
    )


def _resolve_listings(
    connection: Connection, pack: MarketPack, feed: Sequence[store.ListingRow]
) -> list[ResolvedListing]:
    """Apply the listing-level filters, then match and key the listings that pass."""
    screened = [
        ResolvedListing(row, _facts(row), listing_filter_reason(_facts(row), pack.buy_box))
        for row in feed
    ]
    zips = sorted(
        {item.row.zip5 for item in screened if item.filter_reason is None and item.row.zip5}
    )
    if not zips:
        return screened

    index = load_parcel_index(connection, pack.market.id, zips)
    resolved = []
    for item in screened:
        if item.filter_reason is not None:
            resolved.append(item)
            continue
        result = match_listing(index, item.row.zip5, item.row.address_line, item.row.unit)
        resolved.append(replace(item, match=result, ref=_property_ref(item.row, result)))
    return resolved


def _group_by_property(resolved: Sequence[ResolvedListing]) -> dict[str, list[ResolvedListing]]:
    groups: defaultdict[str, list[ResolvedListing]] = defaultdict(list)
    for item in resolved:
        if item.ref is not None:
            groups[item.ref.property_key].append(item)
    return dict(groups)


def _primary(group: Sequence[ResolvedListing], source_priority: Sequence[str]) -> ResolvedListing:
    """The listing that speaks for a property: best source, then lowest listing id."""
    rank = {source: place for place, source in enumerate(source_priority)}
    return min(group, key=lambda item: (rank.get(item.row.source, len(rank)), item.row.id))


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    write: store.CandidateWrite
    # The run date the property was first a candidate, when it already was.
    existing_first_as_of: date | None


def _plan_candidates(
    connection: Connection, market: str, groups: Mapping[str, Sequence[ResolvedListing]]
) -> dict[str, CandidatePlan]:
    refs = {key: group[0].ref for key, group in groups.items() if group[0].ref is not None}
    wanted = set(refs) | {ref.address_key for ref in refs.values()}
    existing = store.candidates_by_key(connection, market, wanted)

    plans = {}
    for key, ref in refs.items():
        found, replaces = existing.get(key), None
        if found is None and ref.address_key != key and ref.address_key in existing:
            found, replaces = existing[ref.address_key], ref.address_key
        plans[key] = CandidatePlan(
            store.CandidateWrite(
                key, ref.account_id, ref.gis_parcel_id, ref.zip5, ref.street_key, replaces
            ),
            found.first_as_of if found else None,
        )
    return plans


def _returning_listing_ids(
    groups: Mapping[str, Sequence[ResolvedListing]],
    plans: Mapping[str, CandidatePlan],
    as_of: date,
) -> set[int]:
    """Listings whose property was a candidate on an earlier run date."""
    return {
        item.row.id
        for key, group in groups.items()
        if (first := plans[key].existing_first_as_of) is not None and first < as_of
        for item in group
    }


def _evaluate(pack: MarketPack, as_of: date, primary: ResolvedListing) -> Evaluation:
    result = primary.match
    if result is None or result.status != "matched":
        return Evaluation("unscored", unscored_reason=result.status if result else "unmatched")
    parcel = aggregate(result)
    if not has_usable_values(parcel):
        return Evaluation("unscored", unscored_reason="values_missing")
    reasons = parcel_filter_reasons(primary.facts, parcel, pack.buy_box)
    if reasons:
        return Evaluation("filtered", filter_reasons=tuple(reasons))
    return Evaluation(
        "ranked", breakdown=score_candidate(pack, as_of, primary.facts, parcel, result)
    )


def _candidate_change_kind(
    group: Sequence[ResolvedListing],
    primary: ResolvedListing,
    kinds: Mapping[int, str],
    in_previous_run: Collection[int],
    candidate_is_new_today: bool,
) -> str:
    if candidate_is_new_today:
        return "new"
    primary_kind = kinds[primary.row.id]
    if primary_kind == "relisted" or not any(item.row.id in in_previous_run for item in group):
        return "relisted"
    return "price_changed" if primary_kind == "price_changed" else "unchanged"


def _build_run(
    connection: Connection,
    pack: MarketPack,
    run_id: int,
    as_of: date,
    sync_status: Literal["fresh", "stale", "skipped"],
) -> RunCounts:
    """Everything one run writes, in the caller's transaction."""
    market = pack.market.id
    store.lock_market_runs(connection, market)
    _refuse_if_out_of_order(connection, market, as_of)
    store.clear_run_rows(connection, run_id)

    window_start, window_end = _window(as_of, pack.market.timezone)
    seen = store.listings_seen_between(connection, market, window_start, window_end)
    feed = [row for row in seen if row.status == "Active"]
    previous = _previous_run(connection, market, as_of)
    previous_ids = set(previous.listings or {})

    resolved = _resolve_listings(connection, pack, feed)
    groups = _group_by_property(resolved)
    plans = _plan_candidates(connection, market, groups)

    classification = diff.classify(
        [diff.FeedListing(row.id, row.price, row.first_seen_at) for row in feed],
        previous.listings,
        _absent_listings(connection, previous.listings, feed),
        window_start=window_start,
        as_of=as_of,
        feed_window_days=_feed_window_days(pack),
        sync_status=sync_status,
        returning=_returning_listing_ids(groups, plans, as_of),
    )
    kinds = {change.listing_id: change.kind for change in classification.changes}

    candidates = store.upsert_candidates(
        connection, market, as_of, [plan.write for plan in plans.values()]
    )
    primaries = {
        key: _primary(group, pack.sourcing.source_priority) for key, group in groups.items()
    }
    evaluations = {key: _evaluate(pack, as_of, primary) for key, primary in primaries.items()}
    ranks = _rank(evaluations, primaries, candidates)

    store.write_matches(
        connection,
        [
            store.ListingMatch(item.row.id, item.match)
            for item in resolved
            if item.match is not None
        ],
    )
    store.write_run_listings(
        connection,
        run_id,
        _run_listing_rows(resolved, primaries, classification.changes, previous.rows),
        candidates,
    )
    store.write_run_candidates(
        connection,
        run_id,
        _run_candidate_rows(
            groups, primaries, evaluations, ranks, candidates, kinds, previous_ids, as_of
        ),
    )
    counts = _count(seen, resolved, classification, previous_ids, evaluations)
    store.complete_run(connection, run_id, counts.model_dump(mode="json"))
    return counts


def _run_candidate_rows(
    groups: Mapping[str, Sequence[ResolvedListing]],
    primaries: Mapping[str, ResolvedListing],
    evaluations: Mapping[str, Evaluation],
    ranks: Mapping[str, int],
    candidates: Mapping[str, store.CandidateRef],
    kinds: Mapping[int, str],
    previous_ids: Collection[int],
    as_of: date,
) -> list[store.RunCandidateWrite]:
    return [
        _run_candidate_row(
            candidates[key],
            primaries[key],
            evaluations[key],
            ranks.get(key),
            _candidate_change_kind(
                group,
                primaries[key],
                kinds,
                previous_ids,
                candidates[key].first_as_of == as_of,
            ),
        )
        for key, group in groups.items()
    ]


@dataclass(frozen=True, slots=True)
class PreviousRun:
    # None when there is no earlier completed run. Otherwise the listings that run still
    # had in its feed (not those it recorded as gone or aged out).
    listings: dict[int, diff.PreviousListing] | None
    rows: dict[int, store.PreviousRunListing]


def _previous_run(connection: Connection, market: str, as_of: date) -> PreviousRun:
    run_id = store.previous_fresh_run(connection, market, as_of)
    if run_id is None:
        return PreviousRun(None, {})
    rows = {row.listing_id: row for row in store.run_listings_of(connection, run_id)}
    still_listed = {
        listing_id: diff.PreviousListing(listing_id, row.price)
        for listing_id, row in rows.items()
        if row.change_kind not in ("gone", "aged_out")
    }
    return PreviousRun(still_listed, rows)


def _absent_listings(
    connection: Connection,
    previous: Mapping[int, diff.PreviousListing] | None,
    feed: Sequence[store.ListingRow],
) -> dict[int, diff.AbsentListing]:
    """The current rows of last run's listings that today's feed does not have."""
    if not previous:
        return {}
    in_feed = {row.id for row in feed}
    rows = store.listings_by_id(connection, [i for i in previous if i not in in_feed])
    return {i: diff.AbsentListing(i, row.status, row.listed_date) for i, row in rows.items()}


def _rank(
    evaluations: Mapping[str, Evaluation],
    primaries: Mapping[str, ResolvedListing],
    candidates: Mapping[str, store.CandidateRef],
) -> dict[str, int]:
    """Rank 1..n over the ranked candidates, by property key."""
    entries = []
    key_of = {}
    for key, evaluation in evaluations.items():
        price = primaries[key].row.price
        if evaluation.breakdown is None or price is None:
            continue
        candidate_id = candidates[key].id
        key_of[candidate_id] = key
        entries.append(RankKey(candidate_id, evaluation.breakdown.total, price))
    return {
        key_of[candidate_id]: place for place, candidate_id in enumerate(rank_order(entries), 1)
    }


def _match_columns(result: MatchResult | None) -> dict[str, str | None]:
    """The run_listing match columns: this run's match, all None when it did not match."""
    if result is None:
        return {"match_status": None, "match_method": None, "match_account_id": None}
    matched = aggregate(result) if result.status == "matched" else None
    return {
        "match_status": result.status,
        "match_method": result.method,
        "match_account_id": matched.account_id if matched else None,
    }


def _run_listing_rows(
    resolved: Sequence[ResolvedListing],
    primaries: Mapping[str, ResolvedListing],
    changes: Sequence[diff.ListingChange],
    previous_rows: Mapping[int, store.PreviousRunListing],
) -> list[store.RunListingWrite]:
    change_of = {change.listing_id: change for change in changes}
    primary_ids = {primary.row.id for primary in primaries.values()}
    rows = []
    for item in resolved:
        change = change_of[item.row.id]
        rows.append(
            store.RunListingWrite(
                listing_id=item.row.id,
                change_kind=change.kind,
                price=change.price,
                prev_price=change.prev_price,
                property_key=item.ref.property_key if item.ref else None,
                candidate_id=None,
                is_primary=item.row.id in primary_ids,
                filter_reason=item.filter_reason,
                **_match_columns(item.match),
            )
        )
    in_feed = {item.row.id for item in resolved}
    for change in changes:
        if change.listing_id in in_feed:
            continue
        before = previous_rows[change.listing_id]
        rows.append(
            store.RunListingWrite(
                listing_id=change.listing_id,
                change_kind=change.kind,
                price=change.price,
                prev_price=change.prev_price,
                property_key=None,
                candidate_id=before.candidate_id,
                is_primary=False,
                filter_reason=before.filter_reason,
                match_status=before.match_status,
                match_method=before.match_method,
                match_account_id=before.match_account_id,
            )
        )
    return rows


def _run_candidate_row(
    candidate: store.CandidateRef,
    primary: ResolvedListing,
    evaluation: Evaluation,
    rank: int | None,
    change_kind: str,
) -> store.RunCandidateWrite:
    breakdown = evaluation.breakdown
    return store.RunCandidateWrite(
        candidate_id=candidate.id,
        primary_listing_id=primary.row.id,
        change_kind=change_kind,
        status=evaluation.status,
        filter_reasons=list(evaluation.filter_reasons),
        unscored_reason=evaluation.unscored_reason,
        score=breakdown.total if breakdown else None,
        rank=rank,
        breakdown=breakdown.model_dump(mode="json") if breakdown else None,
    )


def _match_rate(matched: int, attempted: int) -> str | None:
    if attempted == 0:
        return None
    return str((Decimal(matched) / Decimal(attempted)).quantize(MATCH_RATE_STEP, ROUND_HALF_UP))


def _count(
    seen: Sequence[store.ListingRow],
    resolved: Sequence[ResolvedListing],
    classification: diff.Classification,
    previous_ids: Collection[int],
    evaluations: Mapping[str, Evaluation],
) -> RunCounts:
    in_feed = {item.row.id for item in resolved}
    kinds = [change.kind for change in classification.changes]
    in_feed_kinds = [c.kind for c in classification.changes if c.listing_id in in_feed]
    filtered = Counter(item.filter_reason for item in resolved if item.filter_reason)
    match_status = Counter(item.match.status for item in resolved if item.match)
    attempted = sum(match_status.values())
    return RunCounts(
        listings_seen=len(resolved),
        new=in_feed_kinds.count("new"),
        relisted=in_feed_kinds.count("relisted"),
        price_changed=in_feed_kinds.count("price_changed"),
        unchanged=in_feed_kinds.count("unchanged"),
        gone=kinds.count("gone"),
        aged_out=kinds.count("aged_out"),
        unknown_absent=classification.unknown_absent,
        inactive_ignored=sum(
            1 for row in seen if row.status != "Active" and row.id not in previous_ids
        ),
        listing_filtered=sum(filtered.values()),
        listing_filtered_by_reason=FilteredByReason(
            zip=filtered["zip"], property_type=filtered["property_type"], price=filtered["price"]
        ),
        matched=match_status["matched"],
        ambiguous=match_status["ambiguous"],
        unmatched=match_status["unmatched"],
        match_rate=_match_rate(match_status["matched"], attempted),
        candidates=len(evaluations),
        ranked=sum(1 for e in evaluations.values() if e.status == "ranked"),
        filtered=sum(1 for e in evaluations.values() if e.status == "filtered"),
        unscored=sum(1 for e in evaluations.values() if e.status == "unscored"),
    )

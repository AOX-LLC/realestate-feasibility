"""Value-estimate spend: after a run ranks its candidates, buy a RentCast value estimate for
the top few, within the monthly cap and without eating the budget the daily listing sync
needs.

`plan_spend` decides, from numbers alone, which targets reuse a stored estimate, which are
bought and which wait. `spend_estimates` carries the plan out one address at a time and
commits each answer the moment it arrives, so a crash never loses a paid answer and a retry
reuses it. Nothing here runs inside a database transaction that spans a paid call.

The estimate is not an input to the score: a candidate outside the top N would otherwise be
unrankable. It is stored for the pro-forma.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, Engine

from feasibility.domain.models import ValueEstimate
from feasibility.logging import redact
from feasibility.markets.schema import Estimates
from feasibility.sources.rentcast import budget
from feasibility.sources.rentcast.adapter import to_value_estimate
from feasibility.sources.rentcast.client import (
    BudgetExhaustedError,
    RentCastClient,
    RentCastError,
)
from feasibility.sourcing import estimate_store, store
from feasibility.sourcing.estimate_store import EstimateTarget, EstimateWrite

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpendLimits:
    """What a live run may still spend, measured after the day's sync."""

    # Budget units left in the billing period (`BudgetUsage.remaining`).
    remaining: int
    # Billed value-estimate calls already made in the period, by any caller.
    billed_this_period: int


@dataclass(frozen=True, slots=True)
class SpendPlan:
    reuse: tuple[EstimateTarget, ...]
    call: tuple[EstimateTarget, ...]
    defer: tuple[EstimateTarget, ...]


@dataclass(frozen=True, slots=True)
class EstimateCounts:
    """Every target ends in exactly one of reused, called, deferred or failed; a call that
    RentCast answered with no estimate is a called target that is also counted no_estimate."""

    estimates_targeted: int = 0
    estimates_reused: int = 0
    estimates_called: int = 0
    estimates_no_estimate: int = 0
    estimates_deferred: int = 0
    estimates_failed: int = 0


def _next_period_start(period: date) -> date:
    """The start of the billing period after the one that starts on `period` (anchor days
    are 1-28, so every month has the day)."""
    if period.month == 12:
        return date(period.year + 1, 1, period.day)
    return date(period.year, period.month + 1, period.day)


def spendable_calls(
    wanted: int, policy: Estimates, as_of: date, anchor_day: int, limits: SpendLimits
) -> int:
    """How many of `wanted` estimate calls a live run may make today. Syncs outrank
    estimates: every remaining day of the period keeps `sync_reserve_per_day` units."""
    period = budget.period_start(as_of, anchor_day)
    days_after_today = (_next_period_start(period) - as_of).days - 1
    reserve = days_after_today * policy.sync_reserve_per_day
    cap_left = policy.monthly_cap - limits.billed_this_period
    return max(0, min(wanted, cap_left, limits.remaining - reserve))


def plan_spend(
    targets: Sequence[EstimateTarget],
    last_fetched: Mapping[int, date],
    policy: Estimates,
    as_of: date,
    anchor_day: int,
    limits: SpendLimits | None,
) -> SpendPlan:
    """Split rank-ordered targets into reuse, call and defer.

    `last_fetched` maps a candidate to the date of its newest stored estimate, whatever its
    outcome. One fetched within `ttl_days` of `as_of` is reused. `limits` is None in mock
    mode, where the snapshot answers and nothing is billed: every target without a young
    estimate is called.
    """
    oldest_reusable = as_of - timedelta(days=policy.ttl_days)
    reuse = [t for t in targets if last_fetched.get(t.candidate_id, date.min) >= oldest_reusable]
    reused_ids = {t.candidate_id for t in reuse}
    need_call = [t for t in targets if t.candidate_id not in reused_ids]
    spendable = (
        len(need_call)
        if limits is None
        else spendable_calls(len(need_call), policy, as_of, anchor_day, limits)
    )
    return SpendPlan(tuple(reuse), tuple(need_call[:spendable]), tuple(need_call[spendable:]))


@dataclass(slots=True)
class _Tally:
    reused: int = 0
    called: int = 0
    no_estimate: int = 0
    deferred: int = 0
    failed: int = 0

    def counts(self, targeted: int) -> EstimateCounts:
        return EstimateCounts(
            estimates_targeted=targeted,
            estimates_reused=self.reused,
            estimates_called=self.called,
            estimates_no_estimate=self.no_estimate,
            estimates_deferred=self.deferred,
            estimates_failed=self.failed,
        )


def spend_estimates(
    engine: Engine,
    client: RentCastClient,
    policy: Estimates,
    *,
    run_id: int,
    as_of: date,
    billing_anchor_day: int,
    secrets: Sequence[str],
) -> EstimateCounts:
    """Price the run's top candidates and merge the counts into the run, also when a failure
    cuts the stage short.

    A budget that runs out mid-loop defers the rest. Any other RentCast error defers that one
    candidate, is logged redacted and is not retried: a job retry would spend again. A shape
    change (`SchemaDriftError`) and anything unexpected propagate after the counts are saved.
    """
    with engine.connect() as connection:
        targets = estimate_store.estimate_targets(connection, run_id, policy.top_n)
        stored = estimate_store.latest_estimates(
            connection, [t.candidate_id for t in targets], as_of
        )
        limits = _limits(connection, client, as_of, billing_anchor_day)
    plan = plan_spend(
        targets,
        {candidate_id: row.fetched_on for candidate_id, row in stored.items()},
        policy,
        as_of,
        billing_anchor_day,
        limits,
    )
    tally = _Tally(reused=len(plan.reuse), deferred=len(plan.defer))
    try:
        _call_all(engine, client, plan.call, tally, run_id, as_of, secrets)
    finally:
        counts = tally.counts(len(targets))
        with engine.begin() as connection:
            store.merge_counts(connection, run_id, asdict(counts))
    return counts


def _limits(
    connection: Connection, client: RentCastClient, as_of: date, anchor_day: int
) -> SpendLimits | None:
    if not client.live:
        return None
    period = budget.period_start(as_of, anchor_day)
    usage = client.budget_usage()
    # The client counts in the period of its own clock; if that is not the run's period (a
    # run near midnight at a period boundary) the two numbers describe different periods,
    # so spend nothing rather than guess.
    remaining = usage.remaining if usage.period_start == period else 0
    return SpendLimits(
        remaining=remaining,
        billed_this_period=estimate_store.billed_estimate_calls(connection, period),
    )


def _call_all(
    engine: Engine,
    client: RentCastClient,
    calls: Sequence[EstimateTarget],
    tally: _Tally,
    run_id: int,
    as_of: date,
    secrets: Sequence[str],
) -> None:
    for position, target in enumerate(calls):
        waiting = len(calls) - position - 1
        try:
            fetched = client.value_estimate(target.address.one_line)
            if fetched.stale:
                # The call failed and an old cache entry answered; not a fresh estimate.
                tally.deferred += 1
                continue
            data = None if fetched.data is None else to_value_estimate(fetched.data, target.address)
            with engine.begin() as connection:
                estimate_store.save_estimate(connection, _write(target, data, run_id, as_of))
        except BudgetExhaustedError:
            tally.deferred += waiting + 1
            return
        except RentCastError as error:
            log.warning("value estimate failed: %s", redact(str(error), secrets))
            tally.failed += 1
            continue
        except Exception:
            tally.failed += 1
            tally.deferred += waiting
            raise
        tally.called += 1
        tally.no_estimate += data is None


def _write(
    target: EstimateTarget, estimate: ValueEstimate | None, run_id: int, as_of: date
) -> EstimateWrite:
    address = target.address.one_line
    if estimate is None:
        return EstimateWrite(target.candidate_id, as_of, "no_estimate", address, run_id=run_id)
    return EstimateWrite(
        target.candidate_id,
        as_of,
        "ok",
        address,
        price=estimate.price,
        price_low=estimate.price_low,
        price_high=estimate.price_high,
        comp_count=len(estimate.comparables),
        dropped_comp_count=estimate.dropped_comparables,
        comps=[
            {
                "address": comp.address.one_line,
                "price": str(comp.price),
                "living_area_sqft": comp.living_area_sqft,
                "distance_miles": None if comp.distance_miles is None else str(comp.distance_miles),
                "year_built": comp.year_built,
                "days_old": comp.days_old,
            }
            for comp in estimate.comparables
        ],
        run_id=run_id,
    )

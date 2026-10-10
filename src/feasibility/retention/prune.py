# ruff: noqa: S608  (every statement is built from this module's own constants; values are bound parameters)
"""Retention: delete stored data once it is older than its window, and nothing else.

One function per kind of data, each a bounded loop of short transactions, so a prune never holds a
long lock and a failure part-way leaves a consistent database (the next prune finishes the work).
Every window is a number of days or months before `as_of` (the day the prune runs for); a row is
deleted only when it is strictly older than its cut-off, so a row exactly at the window is kept.

What is never deleted: a market's latest run, a run still being built, a spend row (`llm_call`)
inside its months (the monthly cap and the cost reports read them), and anything a kept row still
refers to. A pruned run keeps its own row, with its counts and error, marked `pruned_at`; its
listings, candidates, pro-formas, signals, narratives, brief and delivery ledger go.

The dry run counts with the same predicates and deletes nothing. It reports what a real prune
would delete, including the listings and candidates that only the deleted runs were holding on to.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, text

from feasibility.config import Settings
from feasibility.sourcing import store as sourcing_store

log = logging.getLogger(__name__)

BATCH = 1000

# What a prune reports, in the order it works.
RULES = (
    "runs",
    "run_listing",
    "run_candidate",
    "proforma",
    "candidate_signals",
    "candidate_narrative",
    "brief",
    "delivery",
    "candidate_estimate",
    "llm_result",
    "llm_call",
    "job",
    "api_cache",
    "listing",
    "candidate",
)
# The tables that hang from a run, deleted with it, children first.
RUN_DETAIL = (
    "delivery",
    "brief",
    "candidate_narrative",
    "candidate_signals",
    "proforma",
    "run_candidate",
    "run_listing",
)


class RetentionRefusedError(ValueError):
    """A prune that was asked to measure from a day of the caller's choosing, where that is not
    allowed. Retrying cannot change it."""


def holds_live_data(engine: Engine) -> bool:
    """Whether this database has ever held live data or live spend: a run made in live mode, or a
    model call that cost money. (Replayed calls are free and are not counted.)"""
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM sourcing_run WHERE data_mode = 'live') "
                    "OR EXISTS (SELECT 1 FROM llm_call WHERE billable)"
                )
            ).scalar_one()
        )


def check_as_of_allowed(
    engine: Engine, settings: Settings, as_of: date | None, dry_run: bool
) -> None:
    """A real prune measured from a chosen day (`--as-of`, or an `as_of` in a queued job) can be
    pointed at a day in the future, which deletes everything past a shorter window, spend records
    included. That is for demonstrating on synthetic data. It is refused with live data in the
    settings, and with a database that has held live data or real spend, whatever the settings
    say now. A dry run, which deletes nothing, may use any day."""
    if as_of is None or dry_run:
        return
    if settings.is_live or holds_live_data(engine):
        raise RetentionRefusedError(
            "a prune from a chosen day is refused here: this database holds live data or spend; "
            "use --dry-run to see what a day would delete"
        )


@dataclass(frozen=True)
class RetentionPolicy:
    run_days: int
    estimate_days: int
    model_cache_days: int
    ledger_months: int
    job_days: int
    listing_days: int


def policy_from(settings: Settings) -> RetentionPolicy:
    return RetentionPolicy(
        run_days=settings.retention_run_days,
        estimate_days=settings.retention_estimate_days,
        model_cache_days=settings.retention_model_cache_days,
        ledger_months=settings.retention_ledger_months,
        job_days=settings.retention_job_days,
        listing_days=settings.retention_listing_days,
    )


@dataclass
class PruneReport:
    as_of: date
    dry_run: bool
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RULES, 0))

    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class Cutoffs:
    """Where each window ends: rows strictly before these are old enough to go."""

    run: date
    estimate: date
    model_cache: datetime
    ledger: datetime
    job: datetime
    listing: datetime
    run_time: datetime

    @classmethod
    def of(cls, policy: RetentionPolicy, as_of: date) -> "Cutoffs":
        def midnight(day: date) -> datetime:
            return datetime.combine(day, time.min, tzinfo=UTC)

        run = as_of - timedelta(days=policy.run_days)
        return cls(
            run=run,
            estimate=as_of - timedelta(days=policy.estimate_days),
            model_cache=midnight(as_of - timedelta(days=policy.model_cache_days)),
            ledger=midnight(months_before(as_of, policy.ledger_months)),
            job=midnight(as_of - timedelta(days=policy.job_days)),
            listing=midnight(as_of - timedelta(days=policy.listing_days)),
            run_time=midnight(run),
        )


def months_before(day: date, months: int) -> date:
    """The same day of the month `months` earlier, or the month's last day if it has none."""
    index = day.year * 12 + day.month - 1 - months
    year, month = index // 12, index % 12 + 1
    for number in range(day.day, 0, -1):
        try:
            return date(year, month, number)
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover


# --- the runs ------------------------------------------------------------------------------------

# A run's detail may go when it is old, not the market's latest, and not still being built: a
# failed run never had stages to finish; a completed one has finished them.
_PRUNABLE_RUNS = """
    SELECT r.id, r.market FROM sourcing_run r
    WHERE r.pruned_at IS NULL
      AND r.as_of < :run_cutoff
      AND (r.status = 'failed' OR (r.status = 'completed' AND r.stages_finished_at IS NOT NULL))
      AND r.as_of < (SELECT max(l.as_of) FROM sourcing_run l WHERE l.market = r.market)
"""


# The newest delivered Notion row of an item is the only record of which page the candidate's row
# lives on. It stays when its run is pruned, so a candidate that comes back is updated at its page
# and not given a second one; it goes with the candidate (see `_prune_candidates`).
_PAGE_RECORD = """
    d.target = 'notion' AND d.status IN ('sent', 'skipped') AND d.remote_ref IS NOT NULL
    AND d.id = (
        SELECT e.id FROM delivery e
        WHERE e.target = 'notion' AND e.item = d.item AND e.mode = d.mode
          AND e.status IN ('sent', 'skipped') AND e.remote_ref IS NOT NULL
        ORDER BY e.updated_at DESC, e.id DESC LIMIT 1
    )
"""


def _count_run_detail(connection: Connection, cutoff: date) -> dict[str, int]:
    found = {
        "runs": connection.execute(
            text(f"SELECT count(*) FROM ({_PRUNABLE_RUNS}) p"),
            {"run_cutoff": cutoff},
        ).scalar_one()
    }
    for table in RUN_DETAIL:
        kept = f" AND NOT ({_PAGE_RECORD})" if table == "delivery" else ""
        found[table] = connection.execute(
            text(
                f"SELECT count(*) FROM {table} d WHERE d.run_id IN "
                f"(SELECT id FROM ({_PRUNABLE_RUNS}) p){kept}"
            ),
            {"run_cutoff": cutoff},
        ).scalar_one()
    return found


def _prune_one_run(engine: Engine, run_id: int, market: str, cutoff: date) -> dict[str, int] | None:
    """Delete one run's detail in one transaction, under its market's run lock. None when the run
    no longer qualifies by the time the lock is held (a run started, or it was pruned)."""
    with engine.begin() as connection:
        sourcing_store.lock_market_runs(connection, market)
        connection.execute(
            text("SELECT id FROM sourcing_run WHERE id = :run FOR UPDATE"), {"run": run_id}
        )
        still = connection.execute(
            text(f"SELECT 1 FROM ({_PRUNABLE_RUNS}) p WHERE p.id = :run"),
            {"run_cutoff": cutoff, "run": run_id},
        ).first()
        if still is None:
            return None
        deleted: dict[str, int] = {"runs": 1}
        for table in RUN_DETAIL:
            kept = f" AND NOT ({_PAGE_RECORD})" if table == "delivery" else ""
            deleted[table] = connection.execute(
                text(f"DELETE FROM {table} AS d WHERE d.run_id = :run{kept}"),
                {"run": run_id},
            ).rowcount
        connection.execute(
            text("UPDATE sourcing_run SET pruned_at = now() WHERE id = :run"), {"run": run_id}
        )
        return deleted


def _prune_runs(engine: Engine, cutoff: date, report: PruneReport) -> None:
    attempted: set[int] = set()
    while True:
        with engine.connect() as connection:
            batch = connection.execute(
                text(f"SELECT id, market FROM ({_PRUNABLE_RUNS}) p ORDER BY id LIMIT :n"),
                {"run_cutoff": cutoff, "n": BATCH},
            ).all()
        batch = [row for row in batch if row.id not in attempted]
        if not batch:
            return
        for row in batch:
            attempted.add(row.id)
            deleted = _prune_one_run(engine, row.id, row.market, cutoff)
            for name, number in (deleted or {}).items():
                report.counts[name] += number


# --- batched deletes -----------------------------------------------------------------------------


def _delete_in_batches(engine: Engine, sql: str, params: Mapping[str, Any]) -> int:
    """Run a `DELETE ... WHERE ctid IN (SELECT ctid ... LIMIT :n)` until it deletes nothing, each
    batch in its own transaction. Returns the rows deleted."""
    total = 0
    while True:
        with engine.begin() as connection:
            deleted = connection.execute(text(sql), {**params, "n": BATCH}).rowcount
        total += deleted
        if deleted < BATCH:
            return total


def _count(engine: Engine, sql: str, params: Mapping[str, Any]) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(sql), params).scalar_one())


_ESTIMATE_OLD = "fetched_on < :cutoff"
_RESULT_OLD = "created_at < :cutoff"
_CALL_OLD = "called_at < :cutoff"
_JOB_OLD = "status IN ('done', 'dead') AND finished_at IS NOT NULL AND finished_at < :cutoff"
_CACHE_OLD = "expires_at < :cutoff"


def _batched_rule(
    engine: Engine, report: PruneReport, name: str, table: str, old: str, cutoff: object
) -> None:
    params = {"cutoff": cutoff}
    if report.dry_run:
        report.counts[name] = _count(
            engine,
            f"SELECT count(*) FROM {table} WHERE {old}",
            params,
        )
    else:
        report.counts[name] = _delete_in_batches(
            engine,
            f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} WHERE {old} LIMIT :n)",
            params,
        )


# --- listings and candidates that only pruned data was holding on to ----------------------------

# Referenced by a run that stays. In a dry run, the runs that would be pruned do not count: they
# are about to go, and the real prune only gets here after they have.
_LISTING_KEPT = """
    l.last_seen_at >= :cutoff
    OR EXISTS (SELECT 1 FROM run_listing x WHERE x.listing_id = l.id {live})
    OR EXISTS (SELECT 1 FROM run_candidate x WHERE x.primary_listing_id = l.id {live})
    OR EXISTS (SELECT 1 FROM candidate_signals x WHERE x.listing_id = l.id {live})
"""
_CANDIDATE_KEPT = """
    c.created_at >= :cutoff
    OR EXISTS (SELECT 1 FROM run_listing x WHERE x.candidate_id = c.id {live})
    OR EXISTS (SELECT 1 FROM run_candidate x WHERE x.candidate_id = c.id {live})
    OR EXISTS (SELECT 1 FROM candidate_estimate x WHERE x.candidate_id = c.id {estimate})
"""


def _live_clause(dry_run: bool) -> str:
    if not dry_run:
        return ""
    return f"AND x.run_id NOT IN (SELECT id FROM ({_PRUNABLE_RUNS}) p)"


def _prune_listings(engine: Engine, cutoffs: Cutoffs, report: PruneReport) -> None:
    kept = _LISTING_KEPT.format(live=_live_clause(report.dry_run))
    params = {"cutoff": cutoffs.listing, "run_cutoff": cutoffs.run}
    if report.dry_run:
        sql = f"SELECT count(*) FROM listing l WHERE NOT ({kept})"
        report.counts["listing"] = _count(engine, sql, params)
        return
    report.counts["listing"] = _delete_in_batches(
        engine,
        "DELETE FROM listing WHERE ctid IN (SELECT l.ctid FROM listing l "
        f"WHERE NOT ({kept}) LIMIT :n)",
        params,
    )


def _prune_candidates(engine: Engine, cutoffs: Cutoffs, report: PruneReport) -> None:
    estimate = "AND x.fetched_on >= :estimate_cutoff"
    kept = _CANDIDATE_KEPT.format(live=_live_clause(report.dry_run), estimate=estimate)
    params = {
        "cutoff": cutoffs.run_time,
        "run_cutoff": cutoffs.run,
        "estimate_cutoff": cutoffs.estimate,
    }
    if report.dry_run:
        report.counts["candidate"] = _count(
            engine, f"SELECT count(*) FROM candidate c WHERE NOT ({kept})", params
        )
        # Their page records go with them: the Notion rows of candidates nothing refers to.
        report.counts["delivery"] += _count(
            engine,
            "SELECT count(*) FROM delivery d WHERE d.target = 'notion' AND d.item IN "
            f"(SELECT 'row:' || c.id FROM candidate c WHERE NOT ({kept}))",
            params,
        )
        return
    while True:
        with engine.begin() as connection:
            gone = [
                row[0]
                for row in connection.execute(
                    text(
                        "DELETE FROM candidate WHERE ctid IN (SELECT c.ctid FROM candidate c "
                        f"WHERE NOT ({kept}) LIMIT :n) RETURNING id"
                    ),
                    {**params, "n": BATCH},
                )
            ]
            if gone:
                report.counts["delivery"] += connection.execute(
                    text("DELETE FROM delivery WHERE target = 'notion' AND item = ANY(:items)"),
                    {"items": [f"row:{number}" for number in gone]},
                ).rowcount
        report.counts["candidate"] += len(gone)
        if len(gone) < BATCH:
            return


# --- the whole prune -----------------------------------------------------------------------------


def prune(
    engine: Engine, policy: RetentionPolicy, *, as_of: date, dry_run: bool = False
) -> PruneReport:
    """Delete (or, with `dry_run`, count) everything older than its window as of `as_of`."""
    cutoffs = Cutoffs.of(policy, as_of)
    report = PruneReport(as_of=as_of, dry_run=dry_run)

    if dry_run:
        with engine.connect() as connection:
            report.counts.update(_count_run_detail(connection, cutoffs.run))
    else:
        _prune_runs(engine, cutoffs.run, report)
    _batched_rule(
        engine, report, "candidate_estimate", "candidate_estimate", _ESTIMATE_OLD, cutoffs.estimate
    )
    _batched_rule(engine, report, "llm_result", "llm_result", _RESULT_OLD, cutoffs.model_cache)
    # Spend rows: only those older than the ledger months, which the monthly cap never reads.
    _batched_rule(engine, report, "llm_call", "llm_call", _CALL_OLD, cutoffs.ledger)
    _batched_rule(engine, report, "job", "job", _JOB_OLD, cutoffs.job)
    _batched_rule(engine, report, "api_cache", "api_cache", _CACHE_OLD, cutoffs.run_time)
    _prune_listings(engine, cutoffs, report)
    _prune_candidates(engine, cutoffs, report)
    log.info(
        "retention %s as of %s: %s",
        "dry run" if dry_run else "prune",
        as_of,
        ", ".join(f"{name} {number}" for name, number in report.counts.items() if number),
    )
    return report

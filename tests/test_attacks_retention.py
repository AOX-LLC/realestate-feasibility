"""Attack tests for retention: a purge deletes what it says and nothing else.

Written before the feature (6b). The three the brief asked for first: a purge never deletes
outside its window; it never touches a spend row (`llm_call`) inside its 13 months; and it removes
a comparable sale's address from both places it is stored (`candidate_estimate.comps` and
`proforma.result`). Until the module exists they fail on the import and are strict `xfail`s; the
session removes the mark when they pass.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from retention_support import (
    AS_OF,
    add_job,
    add_llm_call,
    add_llm_result,
    add_run,
    at,
    count,
    days_ago,
    months_before,
    rows_with,
    run_sql,
    scalar,
)
from sqlalchemy import Engine

pytestmark = pytest.mark.xfail(
    strict=True,
    reason="6b builds retention; the session removes this mark when the tests pass",
)

RUN_TABLES = (
    "run_listing",
    "run_candidate",
    "proforma",
    "candidate_signals",
    "candidate_narrative",
    "brief",
    "delivery",
)


def policy(**changes: int) -> Any:
    from feasibility.retention.prune import RetentionPolicy

    base = {
        "run_days": 90,
        "estimate_days": 90,
        "model_cache_days": 30,
        "ledger_months": 13,
        "job_days": 30,
        "listing_days": 30,
    }
    return RetentionPolicy(**{**base, **changes})


def purge(engine: Engine, *, dry_run: bool = False, **changes: int) -> Any:
    from feasibility.retention.prune import prune

    return prune(engine, policy(**changes), as_of=AS_OF, dry_run=dry_run)


def run_ids(engine: Engine) -> set[int]:
    return {r[0] for r in run_sql(engine, "SELECT id FROM sourcing_run")}


# --- a purge never deletes outside its window ---------------------------------------------------


def test_r1_a_run_is_pruned_only_when_it_is_older_than_the_window(engine: Engine) -> None:
    newest = add_run(engine, age=1, tag="newest")
    inside = {days: add_run(engine, age=days, tag=f"d{days}") for days in (10, 89, 90)}
    outside = {days: add_run(engine, age=days, tag=f"d{days}") for days in (91, 200)}
    before = {t: count(engine, t) for t in RUN_TABLES}

    report = purge(engine)

    # Exactly the two old runs lost their detail, one row each in every run-scoped table.
    for table in RUN_TABLES:
        assert count(engine, table) == before[table] - 2, table
    assert report.counts["runs"] == 2
    for rows in (newest, *inside.values()):
        for table in RUN_TABLES:
            assert count(engine, table, f"run_id = {rows.run_id}") == 1, (rows.run_id, table)
    for rows in outside.values():
        for table in RUN_TABLES:
            assert count(engine, table, f"run_id = {rows.run_id}") == 0, (rows.run_id, table)
        # The run's own summary row stays, marked, with its counts and error.
        summary = run_sql(
            engine,
            "SELECT pruned_at IS NOT NULL, counts->>'candidates', error FROM sourcing_run "
            "WHERE id = :i",
            i=rows.run_id,
        ).one()
        assert tuple(summary) == (True, "1", "kept-error")
    assert run_ids(engine) == {r.run_id for r in (newest, *inside.values(), *outside.values())}


def test_r1_each_other_table_prunes_at_its_own_window_and_not_a_day_sooner(engine: Engine) -> None:
    add_run(engine, age=1, tag="newest")
    for age in (89, 90, 91):
        add_run(engine, age=age, tag=f"e{age}")  # the estimates belong to these runs
    add_llm_result(engine, created=days_ago(29), key="a")
    add_llm_result(engine, created=days_ago(30), key="b")
    add_llm_result(engine, created=days_ago(31), key="c")
    kept_jobs = [add_job(engine, finished=days_ago(29)), add_job(engine, finished=days_ago(30))]
    gone_job = add_job(engine, finished=days_ago(31))
    still_running = add_job(engine, finished=None, status="running")
    old_queued = add_job(engine, finished=None, status="queued")

    purge(engine)

    # candidate_estimate: 89 and 90 days old stay, 91 goes.
    assert {r[0] for r in run_sql(engine, "SELECT fetched_on FROM candidate_estimate")} >= {
        days_ago(1),
        days_ago(89),
        days_ago(90),
    }
    assert count(engine, "candidate_estimate", f"fetched_on = '{days_ago(91)}'") == 0
    # llm_result: 29 and 30 days stay, 31 goes.
    assert count(engine, "llm_result") == 2
    assert count(engine, "llm_result", f"created_at < '{at(days_ago(30))}'") == 0
    # job: only a finished one older than its window; an unfinished one is never touched.
    left = {r[0] for r in run_sql(engine, "SELECT id FROM job")}
    assert left == {*kept_jobs, still_running, old_queued} and gone_job not in left


def test_r1_a_second_prune_and_a_dry_run_delete_nothing(engine: Engine) -> None:
    add_run(engine, age=1, tag="newest")
    add_run(engine, age=120, tag="old")
    add_llm_call(engine, called=months_before(AS_OF, 14))
    snapshot = {t: count(engine, t) for t in (*RUN_TABLES, "candidate_estimate", "llm_call")}

    dry = purge(engine, dry_run=True)
    assert {t: count(engine, t) for t in snapshot} == snapshot  # nothing deleted
    real = purge(engine)
    again = purge(engine)

    assert dry.dry_run is True and dry.counts == real.counts  # it said what it would do
    assert sum(again.counts.values()) == 0
    assert real.counts["runs"] == 1 and real.counts["llm_call"] == 1


def test_r1_the_latest_run_of_a_market_and_a_run_in_progress_are_never_pruned(
    engine: Engine,
) -> None:
    only_old = add_run(engine, age=400, tag="onlyold", market="austin")
    running = add_run(engine, age=200, tag="running", status="running", stages_finished=False)
    unfinished = add_run(engine, age=200, tag="unfinished", stages_finished=False)
    failed = add_run(engine, age=200, tag="failed", status="failed", stages_finished=False)
    add_run(engine, age=1, tag="newest")

    purge(engine)

    for rows in (only_old, running, unfinished):
        assert count(engine, "run_candidate", f"run_id = {rows.run_id}") == 1, rows
        assert scalar(
            engine, "SELECT pruned_at IS NULL FROM sourcing_run WHERE id = :i", i=rows.run_id
        )
    # A failed run never had stages to finish, so it is pruned like any other old run.
    assert count(engine, "run_candidate", f"run_id = {failed.run_id}") == 0


def test_r1_a_pruned_run_is_not_the_run_a_later_diff_compares_against(engine: Engine) -> None:
    from feasibility.sourcing import store

    old = add_run(engine, age=150, tag="old")
    add_run(engine, age=1, tag="newest")
    with engine.connect() as connection:
        assert store.previous_fresh_run(connection, "dallas", days_ago(100)) == old.run_id

    purge(engine)

    with engine.connect() as connection:
        assert store.previous_fresh_run(connection, "dallas", days_ago(100)) is None


# --- a purge never touches a spend row inside its 13 months -------------------------------------


def test_r2_no_llm_call_inside_thirteen_months_is_deleted_or_changed(engine: Engine) -> None:
    edge = months_before(AS_OF, 13)
    inside = [
        add_llm_call(engine, called=AS_OF, cost="0.010000"),
        add_llm_call(engine, called=months_before(AS_OF, 1), cost="0.020000"),
        add_llm_call(engine, called=months_before(AS_OF, 12), cost="0.030000"),
        add_llm_call(engine, called=edge, cost="0.040000"),  # exactly 13 months: kept
    ]
    outside = [
        add_llm_call(engine, called=edge - timedelta(days=1), cost="0.050000"),
        add_llm_call(engine, called=months_before(AS_OF, 20), cost="0.060000"),
    ]
    # A cached result in the window points at a call that is outside it.
    add_llm_result(engine, created=days_ago(5), key="r", call_id=outside[0])
    before = run_sql(
        engine,
        "SELECT id, called_at, cost_usd, reserved_usd, billable, outcome, run_id, mode, "
        "input_tokens FROM llm_call WHERE id = ANY(:ids) ORDER BY id",
        ids=inside,
    ).all()
    total_before = scalar(
        engine, "SELECT sum(cost_usd) FROM llm_call WHERE called_at >= :c", c=at(edge)
    )

    purge(engine)

    after = run_sql(
        engine,
        "SELECT id, called_at, cost_usd, reserved_usd, billable, outcome, run_id, mode, "
        "input_tokens FROM llm_call WHERE id = ANY(:ids) ORDER BY id",
        ids=inside,
    ).all()
    assert after == before  # every column of every row inside the window
    assert {r[0] for r in run_sql(engine, "SELECT id FROM llm_call")} == set(inside)
    assert (
        scalar(engine, "SELECT sum(cost_usd) FROM llm_call WHERE called_at >= :c", c=at(edge))
        == total_before
    )
    # The cached result stays, and no longer points at a call that is gone.
    assert scalar(engine, "SELECT llm_call_id FROM llm_result") is None


def test_r2_the_months_the_spend_caps_read_are_the_same_before_and_after(engine: Engine) -> None:
    for months, cost in ((0, "0.100000"), (1, "0.200000"), (12, "0.300000"), (14, "0.400000")):
        add_llm_call(engine, called=months_before(AS_OF, months), cost=cost)
    sql = (
        "SELECT date_trunc('month', called_at) AS m, sum(cost_usd) FROM llm_call "
        "WHERE billable AND called_at >= :c GROUP BY 1 ORDER BY 1"
    )
    window = at(months_before(AS_OF, 13))
    before = run_sql(engine, sql, c=window).all()

    purge(engine)

    assert run_sql(engine, sql, c=window).all() == before
    assert count(engine, "llm_call") == 3


# --- comparable sales' addresses leave both places they are stored ------------------------------


def test_r3_comp_addresses_are_removed_from_the_estimate_and_from_the_pro_forma(
    engine: Engine,
) -> None:
    fresh = add_run(engine, age=3, tag="fresh")
    old = add_run(engine, age=100, tag="stale")
    assert set(rows_with(engine, old.comp_street)) >= {"candidate_estimate", "proforma"}

    purge(engine)

    # Not one table holds the old address any more, whichever column it was in...
    assert rows_with(engine, old.comp_street) == {}
    assert rows_with(engine, old.quote) == {}  # ...nor the signal quote beside it
    assert rows_with(engine, "remarks-stale") == {}  # nor the listing's remarks text
    # ...and the recent run's are still there, in both places.
    assert set(rows_with(engine, fresh.comp_street)) >= {"candidate_estimate", "proforma"}


def test_r3_an_estimate_is_removed_with_its_comps_even_when_its_run_is_inside_the_window(
    engine: Engine,
) -> None:
    add_run(engine, age=1, tag="newest")
    kept = add_run(engine, age=40, tag="kept")
    run_sql(
        engine,
        "UPDATE candidate_estimate SET fetched_on = :d WHERE candidate_id = :c",
        d=days_ago(120),
        c=kept.candidate_id,
    )

    purge(engine)

    assert count(engine, "candidate_estimate", f"candidate_id = {kept.candidate_id}") == 0
    assert count(engine, "run_candidate", f"run_id = {kept.run_id}") == 1  # the run keeps its rows


def test_r3_a_listing_nothing_refers_to_loses_its_remarks_and_one_a_run_refers_to_keeps_them(
    engine: Engine,
) -> None:
    add_run(engine, age=1, tag="newest")
    kept = add_run(engine, age=60, tag="kept")
    gone = add_run(engine, age=100, tag="gone")
    run_sql(  # kept's listing was last seen long ago, but a run inside the window refers to it
        engine,
        "UPDATE listing SET last_seen_at = :t WHERE id = :i",
        t=at(days_ago(70)),
        i=kept.listing_id,
    )

    purge(engine)

    assert count(engine, "listing", f"id = {kept.listing_id}") == 1
    assert count(engine, "listing", f"id = {gone.listing_id}") == 0
    assert count(engine, "candidate", f"id = {gone.candidate_id}") == 0
    assert count(engine, "candidate", f"id = {kept.candidate_id}") == 1


def test_r4_settings_below_their_minimum_are_refused() -> None:
    from pydantic import ValidationError

    from feasibility.config import Settings

    for name, too_low in (
        ("RETENTION_RUN_DAYS", "7"),
        ("RETENTION_ESTIMATE_DAYS", "29"),
        ("RETENTION_MODEL_CACHE_DAYS", "0"),
        ("RETENTION_LEDGER_MONTHS", "1"),
        ("RETENTION_JOB_DAYS", "0"),
        ("RETENTION_LISTING_DAYS", "0"),
    ):
        with pytest.raises(ValidationError) as raised:
            Settings(_env_file=None, **{name.lower(): too_low})  # type: ignore[arg-type]
        assert name.lower() in str(raised.value).lower()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.retention_run_days, settings.retention_estimate_days) == (90, 90)
    assert (settings.retention_model_cache_days, settings.retention_ledger_months) == (30, 13)


def test_r5_a_prune_for_one_market_leaves_another_markets_latest_run_alone(engine: Engine) -> None:
    add_run(engine, age=1, tag="dallas-new")
    other_latest = add_run(engine, age=300, tag="austin-only", market="austin")
    other_old = add_run(engine, age=500, tag="austin-older", market="austin")
    run_sql(
        engine,
        "UPDATE sourcing_run SET as_of = :d WHERE id = :i",
        d=date(2025, 1, 1),
        i=other_old.run_id,
    )

    purge(engine)

    assert count(engine, "run_candidate", f"run_id = {other_latest.run_id}") == 1
    assert count(engine, "run_candidate", f"run_id = {other_old.run_id}") == 0

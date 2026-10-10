"""Builders for the retention tests: runs, estimates, model rows and jobs of a chosen age.

Everything is inserted with plain SQL, so each row's date is exactly what the test says. `AS_OF`
is the day the prune "runs"; an `age` is a number of days before it."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Engine, text

AS_OF = date(2026, 12, 15)
DIGEST = "e" * 64


def months_before(day: date, months: int) -> date:
    """The same day of the month `months` earlier (the days used here never exceed 28)."""
    index = day.year * 12 + day.month - 1 - months
    return date(index // 12, index % 12 + 1, day.day)


def at(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=UTC)


def days_ago(age: int) -> date:
    return AS_OF - timedelta(days=age)


@dataclass(frozen=True)
class RunRows:
    run_id: int
    candidate_id: int
    listing_id: int
    comp_street: str
    quote: str


def run_sql(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.begin() as connection:
        return connection.execute(text(sql), params)


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    """One value; committed, so an INSERT ... RETURNING here is kept."""
    with engine.begin() as connection:
        return connection.execute(text(sql), params).scalar_one()


def add_run(
    engine: Engine,
    *,
    age: int,
    tag: str,
    market: str = "dallas",
    status: str = "completed",
    stages_finished: bool = True,
) -> RunRows:
    """A run `age` days old with one ranked candidate and one row in every run-scoped table: its
    listing and match, a computed pro-forma whose result holds a comp address, signals holding a
    quote, an accepted narrative, a brief, delivery rows, and the value estimate (with the same
    comp address) it was built from."""
    as_of = days_ago(age)
    comp = f"{tag.upper()} COMP ST"
    quote = f"quote-{tag}"
    with engine.begin() as c:
        run_id = c.execute(
            text(
                "INSERT INTO sourcing_run (market, as_of, status, sync_status, counts, error, "
                "started_at, finished_at, stages_finished_at) VALUES (:m, :d, :s, 'fresh', "
                "CAST(:counts AS jsonb), 'kept-error', :t, :t, :f) RETURNING id"
            ),
            {
                "m": market,
                "d": as_of,
                "s": status,
                "counts": '{"candidates": 1}',
                "t": at(as_of),
                "f": at(as_of) if stages_finished else None,
            },
        ).scalar_one()
        candidate_id = c.execute(
            text(
                "INSERT INTO candidate (market, property_key, street_key, first_as_of, created_at) "
                "VALUES (:m, :k, :k, :d, :t) RETURNING id"
            ),
            {"m": market, "k": f"acct:{tag}", "d": as_of, "t": at(as_of)},
        ).scalar_one()
        listing_id = c.execute(
            text(
                "INSERT INTO listing (source, external_id, market, address_line, remarks, "
                "first_seen_at, last_seen_at, raw) VALUES ('mls', :e, :m, :a, :r, :t, :t, "
                "CAST('{}' AS jsonb)) RETURNING id"
            ),
            {
                "e": f"l-{tag}",
                "m": market,
                "a": f"{tag.upper()} MAIN ST",
                "r": f"remarks-{tag}",
                "t": at(as_of),
            },
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO run_listing (run_id, listing_id, change_kind, candidate_id, "
                "is_primary) VALUES (:r, :l, 'new', :c, true)"
            ),
            {"r": run_id, "l": listing_id, "c": candidate_id},
        )
        c.execute(
            text(
                "INSERT INTO run_candidate (run_id, candidate_id, primary_listing_id, "
                "change_kind, status, score, rank, breakdown) VALUES (:r, :c, :l, 'new', "
                "'ranked', 50, 1, CAST('{}' AS jsonb))"
            ),
            {"r": run_id, "c": candidate_id, "l": listing_id},
        )
        result = f'{{"arv": {{"comps": [{{"address": "{comp}", "price": "1.00"}}]}}}}'
        c.execute(
            text(
                "INSERT INTO proforma (run_id, candidate_id, status, estimate_fetched_on, "
                "offer_price, arv, total_cost, profit, margin, result) VALUES (:r, :c, "
                "'computed', :d, 100, 200, 150, 50, 0.25, CAST(:j AS jsonb))"
            ),
            {"r": run_id, "c": candidate_id, "d": as_of, "j": result},
        )
        c.execute(
            text(
                "INSERT INTO candidate_signals (run_id, candidate_id, status, listing_id, result) "
                "VALUES (:r, :c, 'extracted', :l, CAST(:j AS jsonb))"
            ),
            {"r": run_id, "c": candidate_id, "l": listing_id, "j": f'{{"quote": "{quote}"}}'},
        )
        c.execute(
            text(
                "INSERT INTO candidate_narrative (run_id, candidate_id, status, input_sha256, "
                "result) VALUES (:r, :c, 'accepted', :h, CAST('{}' AS jsonb))"
            ),
            {"r": run_id, "c": candidate_id, "h": DIGEST},
        )
        c.execute(
            text(
                "INSERT INTO brief (run_id, version, completeness, content, content_sha256) "
                "VALUES (:r, 1, 'complete', CAST(:j AS jsonb), :h)"
            ),
            {"r": run_id, "j": f'{{"street": "{tag.upper()} MAIN ST"}}', "h": DIGEST},
        )
        for target, item in (("slack", "digest"), ("notion", f"row:{candidate_id}")):
            c.execute(
                text(
                    "INSERT INTO delivery (run_id, target, item, mode, status, content_sha256, "
                    "remote_ref) VALUES (:r, :t, :i, 'mock', 'sent', :h, 'ref-1')"
                ),
                {"r": run_id, "t": target, "i": item, "h": DIGEST},
            )
        c.execute(
            text(
                "INSERT INTO candidate_estimate (candidate_id, fetched_on, outcome, address, "
                "price, comp_count, comps, run_id) VALUES (:c, :d, 'ok', :a, 100, 1, "
                "CAST(:j AS jsonb), :r)"
            ),
            {
                "c": candidate_id,
                "d": as_of,
                "a": f"{tag.upper()} MAIN ST",
                "j": f'[{{"address": "{comp}", "price": "1.00"}}]',
                "r": run_id,
            },
        )
    return RunRows(run_id, candidate_id, listing_id, comp, quote)


def add_llm_call(engine: Engine, *, called: date, cost: str = "0.010000") -> int:
    return int(
        scalar(
            engine,
            "INSERT INTO llm_call (stage, prompt_id, prompt_version, input_sha256, tier, model, "
            "mode, billable, outcome, cost_usd, reserved_usd, called_at) VALUES ('narrative', "
            "'narrative.write', 1, :h, 'mid', 'm', 'live', true, 'ok', :cost, 0.05, :t) "
            "RETURNING id",
            h=DIGEST,
            cost=cost,
            t=at(called),
        )
    )


def add_llm_result(engine: Engine, *, created: date, key: str, call_id: int | None = None) -> None:
    run_sql(
        engine,
        "INSERT INTO llm_result (prompt_id, prompt_version, tier, input_sha256, result, "
        "llm_call_id, created_at) VALUES ('narrative.write', 1, 'mid', :h, "
        "CAST(:j AS jsonb), :c, :t)",
        h=(key * 64)[:64],
        j=f'{{"quote": "cached-{key}"}}',
        c=call_id,
        t=at(created),
    )


def add_job(engine: Engine, *, finished: date | None, status: str = "done") -> int:
    return int(
        scalar(
            engine,
            "INSERT INTO job (kind, payload, status, finished_at) VALUES ('sourcing.run', "
            "CAST('{}' AS jsonb), :s, :t) RETURNING id",
            s=status,
            t=None if finished is None else at(finished),
        )
    )


def count(engine: Engine, table: str, where: str = "true") -> int:
    # Table names here come from the tests themselves.
    return int(scalar(engine, f"SELECT count(*) FROM {table} WHERE {where}"))  # noqa: S608


def rows_with(engine: Engine, needle: str) -> dict[str, int]:
    """Per table, how many rows hold `needle` anywhere in their text."""
    found: dict[str, int] = {}
    names = [
        r[0]
        for r in run_sql(
            engine,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    ]
    for name in names:
        hits = scalar(
            engine,
            f'SELECT count(*) FROM "{name}" t WHERE t::text LIKE :n',  # noqa: S608
            n=f"%{needle}%",
        )
        if hits:
            found[name] = int(hits)
    return found

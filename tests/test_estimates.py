"""Value-estimate spend: the pure plan (reuse, cap, reserve) and the loop that carries it out
against a stub transport. Nothing here reaches RentCast; the live path runs only through
`StubTransport`, which answers from the committed synthetic snapshot."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Engine, delete, func, insert, select, text

from feasibility.config import DataMode, Settings
from feasibility.domain.address import Address
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import Estimates
from feasibility.snapshot.load import seed
from feasibility.sources.rentcast import budget
from feasibility.sources.rentcast.client import RentCastClient, SchemaDriftError, Ttls
from feasibility.sources.rentcast.transport import SnapshotTransport, TransportResponse
from feasibility.sourcing import estimate_store
from feasibility.sourcing import store as run_store
from feasibility.sourcing.estimate_store import EstimateTarget
from feasibility.sourcing.estimates import (
    SPEND_LOCK,
    EstimateCounts,
    SpendLimits,
    plan_spend,
    spend_estimates,
)
from feasibility.sourcing.run import run_sourcing
from feasibility.tables import api_budget, api_request_log, candidate_estimate, sourcing_run

DAY_ONE = date(2026, 10, 1)
POLICY = get_pack("dallas").sourcing.estimates
TTLS = Ttls(timedelta(hours=20), timedelta(days=30), timedelta(days=7))
SNAPSHOT = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


def _targets(count: int = 5) -> list[EstimateTarget]:
    return [
        EstimateTarget(100 + rank, rank, Address(street=f"{rank} TEST ST", zip5="75201"))
        for rank in range(1, count + 1)
    ]


# --- plan_spend, from numbers alone -----------------------------------------------------


def _spendable(as_of: date, *, remaining: int, billed: int = 0, anchor: int = 1) -> int:
    plan = plan_spend(_targets(), {}, POLICY, as_of, anchor, SpendLimits(remaining, billed))
    return len(plan.call)


class Month(NamedTuple):
    name: str
    as_of: date
    anchor: int
    days_after_today: int


MONTHS = [
    Month("31 days, first day", date(2026, 10, 1), 1, 30),
    Month("31 days, last day", date(2026, 10, 31), 1, 0),
    Month("30 days, first day", date(2026, 11, 1), 1, 29),
    Month("28 days, first day", date(2027, 2, 1), 1, 27),
    Month("year end", date(2026, 12, 20), 1, 11),
    Month("anchor 15, first day", date(2026, 10, 15), 15, 30),
    Month("anchor 15, day before renewal", date(2026, 11, 14), 15, 0),
    Month("anchor 15, across a year", date(2026, 12, 20), 15, 25),
]


@pytest.mark.parametrize("month", MONTHS, ids=lambda month: month.name)
def test_the_reserve_holds_back_one_unit_for_each_remaining_day(month: Month) -> None:
    reserve = month.days_after_today * POLICY.sync_reserve_per_day
    # Exactly the reserve left: nothing to spend. One more unit buys one estimate.
    assert _spendable(month.as_of, remaining=reserve, anchor=month.anchor) == 0
    assert _spendable(month.as_of, remaining=reserve + 1, anchor=month.anchor) == 1
    assert _spendable(month.as_of, remaining=reserve + 3, anchor=month.anchor) == 3


def test_the_worked_day_spends_five() -> None:
    """31-day month, as_of the 1st: 49 left after the sync, reserve 30, 19 spendable, so the
    five targets (not the cap of 20) are what binds."""
    assert _spendable(date(2026, 10, 1), remaining=49) == 5


def test_the_last_day_keeps_no_reserve() -> None:
    assert _spendable(date(2026, 10, 31), remaining=5) == 5


def test_a_near_exhausted_budget_buys_what_the_reserve_leaves() -> None:
    """Budget 10, 4 used after the sync: remaining 6; Oct 27 has 4 days after it, so 2."""
    assert _spendable(date(2026, 10, 27), remaining=6) == 2
    assert _spendable(date(2026, 10, 27), remaining=0) == 0
    assert _spendable(date(2026, 10, 27), remaining=3) == 0


def test_the_monthly_cap_binds_before_the_budget() -> None:
    last_day = date(2026, 10, 31)
    assert _spendable(last_day, remaining=50, billed=20) == 0
    assert _spendable(last_day, remaining=50, billed=21) == 0
    assert _spendable(last_day, remaining=50, billed=18) == 2


def test_the_plan_defers_what_it_cannot_buy_in_rank_order() -> None:
    plan = plan_spend(
        _targets(),
        {},
        POLICY,
        date(2026, 10, 27),
        1,
        SpendLimits(remaining=6, billed_this_period=0),
    )

    assert [t.rank for t in plan.call] == [1, 2]
    assert [t.rank for t in plan.defer] == [3, 4, 5]
    assert plan.reuse == ()


def test_a_young_estimate_is_reused_up_to_and_including_ttl_days() -> None:
    as_of = date(2026, 10, 20)
    last_fetched = {
        101: as_of,
        102: as_of - timedelta(days=POLICY.ttl_days - 1),
        103: as_of - timedelta(days=POLICY.ttl_days),
        104: as_of - timedelta(days=POLICY.ttl_days + 1),
    }

    plan = plan_spend(_targets(), last_fetched, POLICY, as_of, 1, SpendLimits(50, 0))

    assert [t.rank for t in plan.reuse] == [1, 2, 3]
    assert [t.rank for t in plan.call] == [4, 5]


def test_mock_mode_plans_a_call_for_every_target_without_a_young_estimate() -> None:
    tiny = Estimates(top_n=5, monthly_cap=5, ttl_days=7, sync_reserve_per_day=1)

    plan = plan_spend(_targets(), {101: date(2026, 10, 1)}, tiny, date(2026, 10, 2), 1, None)

    assert [t.rank for t in plan.call] == [2, 3, 4, 5]
    assert plan.defer == ()


# --- spend_estimates, through a stub transport ------------------------------------------


class StubTransport:
    """Answers `/avm/value` from the snapshot, records every request, and lets a test make
    one address fail or run code at the Nth call."""

    def __init__(self) -> None:
        self.addresses: list[str] = []
        self.responses: dict[str, TransportResponse] = {}
        self.before_call: dict[int, Callable[[], None]] = {}
        self._snapshot = SnapshotTransport(SNAPSHOT.snapshot_dir / "rentcast", None)

    def get(self, path: str, params: Mapping[str, str]) -> TransportResponse:
        assert path == "/avm/value"
        address = params["address"]
        self.addresses.append(address)
        if (hook := self.before_call.get(len(self.addresses))) is not None:
            hook()
        return self.responses.get(address) or self._snapshot.get(path, params)

    def close(self) -> None:
        pass


class Spend:
    """One day-one run with no estimates yet, and a live client over a stub transport."""

    def __init__(self, engine: Engine, *, monthly_budget: int = 50, use_cache: bool = False):
        seed(engine, SNAPSHOT)
        self.engine = engine
        self.run_id = run_sourcing(engine, SNAPSHOT, "dallas", DAY_ONE).run_id
        with engine.begin() as connection:
            connection.execute(delete(candidate_estimate))
            connection.execute(delete(api_request_log))
            connection.execute(delete(api_budget))
        self.transport = StubTransport()
        self.now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        self.monthly_budget = monthly_budget
        self.use_cache = use_cache
        with engine.connect() as connection:
            self.targets = estimate_store.estimate_targets(connection, self.run_id, 5)

    def client(self) -> RentCastClient:
        return RentCastClient(
            self.engine,
            self.transport,
            live=True,
            monthly_budget=self.monthly_budget,
            billing_anchor_day=1,
            ttls=TTLS,
            use_cache=self.use_cache,
            clock=lambda: self.now,
        )

    def spend(self, as_of: date | None = None) -> EstimateCounts:
        day = as_of or self.now.date()
        self.now = datetime.combine(day, self.now.time(), tzinfo=UTC)
        return spend_estimates(
            self.engine,
            self.client(),
            POLICY,
            run_id=self.run_id,
            as_of=day,
            billing_anchor_day=1,
            secrets=["sentinel-key"],
        )

    def used(self, period: date = date(2026, 10, 1)) -> int:
        with self.engine.connect() as connection:
            return budget.usage(connection, "rentcast", period, self.monthly_budget).used

    def set_used(self, used: int, period: date = date(2026, 10, 1)) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO api_budget (provider, period_start, request_limit, used) "
                    "VALUES ('rentcast', :p, :l, :u) ON CONFLICT (provider, period_start) "
                    "DO UPDATE SET used = :u, request_limit = :l"
                ),
                {"p": period, "l": self.monthly_budget, "u": used},
            )

    def stored(self) -> list[tuple[int, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(candidate_estimate.c.candidate_id, candidate_estimate.c.outcome).order_by(
                    candidate_estimate.c.candidate_id
                )
            )
            return [(row.candidate_id, row.outcome) for row in rows]

    def run_counts(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            return connection.execute(
                select(sourcing_run.c.counts).where(sourcing_run.c.id == self.run_id)
            ).scalar_one()

    def one_lines(self) -> list[str]:
        return [target.address.one_line for target in self.targets]


@pytest.fixture
def spend(engine: Engine) -> Spend:
    return Spend(engine)


def _assert_every_target_is_accounted_for(counts: EstimateCounts) -> None:
    assert counts.estimates_targeted == (
        counts.estimates_reused
        + counts.estimates_called
        + counts.estimates_deferred
        + counts.estimates_failed
    )


def test_five_targets_with_plenty_of_budget_make_exactly_five_calls_in_rank_order(
    spend: Spend,
) -> None:
    assert len(spend.targets) == 5

    counts = spend.spend()

    assert spend.transport.addresses == spend.one_lines()
    assert counts == EstimateCounts(estimates_targeted=5, estimates_called=5)
    assert [outcome for _, outcome in spend.stored()] == ["ok"] * 5
    assert spend.used() == 5
    assert spend.run_counts()["estimates_called"] == 5


def test_a_stored_estimate_keeps_its_comps_and_the_run_that_bought_it(spend: Spend) -> None:
    spend.spend()

    with spend.engine.connect() as connection:
        stored = estimate_store.latest_estimates(
            connection, [spend.targets[0].candidate_id], DAY_ONE
        )[spend.targets[0].candidate_id]
    assert stored.address == spend.one_lines()[0]
    assert (stored.comp_count, stored.dropped_comp_count, stored.run_id) == (5, 2, spend.run_id)
    assert len(stored.comps) == 5
    assert set(stored.comps[0]) == {
        "address",
        "price",
        "living_area_sqft",
        "distance_miles",
        "year_built",
        "days_old",
    }


def test_a_near_exhausted_budget_buys_the_first_two_and_defers_the_rest(spend: Spend) -> None:
    spend.monthly_budget = 10
    spend.set_used(4)

    counts = spend.spend(date(2026, 10, 27))

    assert spend.transport.addresses == spend.one_lines()[:2]
    assert counts == EstimateCounts(estimates_targeted=5, estimates_called=2, estimates_deferred=3)
    assert [cid for cid, _ in spend.stored()] == sorted(t.candidate_id for t in spend.targets[:2])


def test_a_spent_budget_makes_no_calls_and_defers_everything(spend: Spend) -> None:
    spend.monthly_budget = 10
    spend.set_used(10)

    counts = spend.spend(date(2026, 10, 27))

    assert spend.transport.addresses == []
    assert counts == EstimateCounts(estimates_targeted=5, estimates_deferred=5)
    assert spend.stored() == []


def _log_estimate_calls(engine: Engine, count: int, *, billed: bool, period: date) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(api_request_log),
            [
                {
                    "provider": "rentcast",
                    "endpoint": "/avm/value",
                    "request_key": f"k{index}",
                    "period_start": period,
                    "outcome": "ok" if billed else "cache_hit",
                    "billed": billed,
                }
                for index in range(count)
            ],
        )


def test_twenty_billed_estimates_this_period_stop_the_spend_although_budget_remains(
    spend: Spend,
) -> None:
    _log_estimate_calls(spend.engine, 20, billed=True, period=date(2026, 10, 1))

    counts = spend.spend()

    assert spend.transport.addresses == []
    assert counts.estimates_deferred == 5
    assert spend.used() == 0


def test_cache_hits_and_other_periods_do_not_count_toward_the_cap(spend: Spend) -> None:
    _log_estimate_calls(spend.engine, 20, billed=False, period=date(2026, 10, 1))
    _log_estimate_calls(spend.engine, 20, billed=True, period=date(2026, 9, 1))

    counts = spend.spend()

    assert counts.estimates_called == 5


def test_the_cap_leaves_room_for_the_estimates_not_yet_bought(spend: Spend) -> None:
    _log_estimate_calls(spend.engine, 18, billed=True, period=date(2026, 10, 1))

    counts = spend.spend()

    assert (counts.estimates_called, counts.estimates_deferred) == (2, 3)


def test_the_same_day_again_makes_no_calls(spend: Spend) -> None:
    spend.spend()
    spend.transport.addresses.clear()

    counts = spend.spend()

    assert spend.transport.addresses == []
    assert counts == EstimateCounts(estimates_targeted=5, estimates_reused=5)


def test_an_estimate_is_reused_for_ttl_days_and_bought_again_after(spend: Spend) -> None:
    spend.spend()
    spend.transport.addresses.clear()

    assert spend.spend(DAY_ONE + timedelta(days=6)).estimates_reused == 5
    assert spend.spend(DAY_ONE + timedelta(days=7)).estimates_reused == 5
    assert spend.transport.addresses == []

    again = spend.spend(DAY_ONE + timedelta(days=8))

    assert (again.estimates_reused, again.estimates_called) == (0, 5)
    assert spend.transport.addresses == spend.one_lines()


def test_a_not_found_estimate_is_stored_refunded_and_reused_like_any_other(spend: Spend) -> None:
    first = spend.one_lines()[0]
    spend.transport.responses[first] = TransportResponse(404, {"error": "not found"})

    counts = spend.spend()

    assert counts == EstimateCounts(
        estimates_targeted=5, estimates_called=5, estimates_no_estimate=1
    )
    assert (spend.targets[0].candidate_id, "no_estimate") in spend.stored()
    assert spend.used() == 4  # the 404 was refunded; the four answers were billed
    spend.transport.addresses.clear()

    assert spend.spend().estimates_reused == 5
    assert spend.transport.addresses == []


def test_a_budget_that_runs_out_mid_loop_defers_the_rest(spend: Spend) -> None:
    last_day = date(2026, 10, 31)

    def spend_the_rest_of_the_budget() -> None:
        spend.set_used(spend.monthly_budget, period=date(2026, 10, 1))

    spend.transport.before_call[2] = spend_the_rest_of_the_budget

    counts = spend.spend(last_day)

    assert spend.transport.addresses == spend.one_lines()[:2]
    assert counts == EstimateCounts(estimates_targeted=5, estimates_called=2, estimates_deferred=3)
    _assert_every_target_is_accounted_for(counts)


def test_a_failed_estimate_is_not_retried_and_the_next_one_is_still_called(spend: Spend) -> None:
    second = spend.one_lines()[1]
    spend.transport.responses[second] = TransportResponse(500, {"error": "boom"})

    counts = spend.spend()

    assert spend.transport.addresses == spend.one_lines()
    assert counts == EstimateCounts(estimates_targeted=5, estimates_called=4, estimates_failed=1)
    assert [cid for cid, _ in spend.stored()] == sorted(
        t.candidate_id for i, t in enumerate(spend.targets) if i != 1
    )
    _assert_every_target_is_accounted_for(counts)


def test_schema_drift_propagates_after_the_counts_are_saved_and_a_retry_reuses_what_was_bought(
    spend: Spend,
) -> None:
    second = spend.one_lines()[1]
    spend.transport.responses[second] = TransportResponse(200, {"price": "not a number"})

    with pytest.raises(SchemaDriftError):
        spend.spend()

    assert spend.transport.addresses == spend.one_lines()[:2]
    assert [cid for cid, _ in spend.stored()] == [spend.targets[0].candidate_id]
    saved = spend.run_counts()
    assert (saved["estimates_called"], saved["estimates_failed"]) == (1, 1)
    assert saved["estimates_deferred"] == 3

    spend.transport.responses.clear()
    spend.transport.addresses.clear()
    retry = spend.spend()

    assert spend.transport.addresses == spend.one_lines()[1:]
    assert (retry.estimates_reused, retry.estimates_called) == (1, 4)


def test_an_old_cache_answer_after_a_failed_call_is_deferred_and_not_stored(engine: Engine) -> None:
    spend = Spend(engine, use_cache=True)
    spend.spend()
    with engine.begin() as connection:
        connection.execute(text("UPDATE api_cache SET expires_at = now() - interval '1 second'"))
        connection.execute(delete(candidate_estimate))
    for address in spend.one_lines():
        spend.transport.responses[address] = TransportResponse(500, {"error": "down"})

    counts = spend.spend()

    assert counts == EstimateCounts(estimates_targeted=5, estimates_deferred=5, estimates_called=0)
    assert spend.stored() == []


def test_the_error_is_redacted_in_the_log(spend: Spend, caplog: pytest.LogCaptureFixture) -> None:
    second = spend.one_lines()[1]
    spend.transport.responses[second] = TransportResponse(500, {"error": "sentinel-key"})

    with caplog.at_level("WARNING"):
        spend.spend()

    assert "value estimate failed" in caplog.text
    assert "sentinel-key" not in caplog.text


def test_a_mock_client_buys_every_target_and_never_touches_the_budget(engine: Engine) -> None:
    spend = Spend(engine)
    mock_client = RentCastClient.from_settings(engine, SNAPSHOT, use_cache=False)

    counts = spend_estimates(
        engine,
        mock_client,
        POLICY,
        run_id=spend.run_id,
        as_of=DAY_ONE,
        billing_anchor_day=1,
        secrets=[],
    )

    assert counts == EstimateCounts(estimates_targeted=5, estimates_called=5)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(api_budget)).scalar_one() == 0


def test_a_client_in_another_period_than_the_run_spends_nothing(spend: Spend) -> None:
    """Dallas is still on Oct 31 while the client's UTC clock is already in the next period:
    the budget units it reports belong to a different period than the cap's rows, so the
    stage defers instead of guessing."""
    spend.now = datetime(2026, 11, 1, 1, tzinfo=UTC)

    counts = spend_estimates(
        spend.engine,
        spend.client(),
        POLICY,
        run_id=spend.run_id,
        as_of=date(2026, 10, 31),
        billing_anchor_day=1,
        secrets=[],
    )

    assert spend.transport.addresses == []
    assert counts == EstimateCounts(estimates_targeted=5, estimates_deferred=5)


def test_a_fresh_cache_answer_keeps_its_date_and_is_not_counted_as_bought(engine: Engine) -> None:
    """Another caller (verify-rentcast) priced the address on Sept 28 with the same request;
    the client answers from its cache with no bill. The row says Sept 28, not today."""
    spend = Spend(engine, use_cache=True)
    spend.spend()
    with engine.begin() as connection:
        connection.execute(delete(candidate_estimate))
        connection.execute(text("UPDATE api_cache SET fetched_at = '2026-09-28 12:00+00'"))
    spend.transport.addresses.clear()

    counts = spend.spend(date(2026, 10, 1))

    assert spend.transport.addresses == []
    assert counts == EstimateCounts(estimates_targeted=5, estimates_reused=5)
    with engine.connect() as connection:
        dates = set(connection.execute(select(candidate_estimate.c.fetched_on)).scalars())
    assert dates == {date(2026, 9, 28)}


def test_only_one_stage_spends_at_a_time(spend: Spend) -> None:
    """While a stage is between reading the headroom and finishing its calls, another stage
    cannot take the spend lock, so it cannot read the same headroom."""
    seen: list[bool] = []

    def try_to_take_the_lock() -> None:
        with spend.engine.connect() as other:
            taken = other.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), SPEND_LOCK
            ).scalar_one()
            if taken:
                other.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), SPEND_LOCK
                )
            seen.append(bool(taken))

    spend.transport.before_call[2] = try_to_take_the_lock
    spend.spend()

    assert seen == [False]
    with spend.engine.connect() as other:
        # Released again afterwards: the pool would otherwise keep the session lock.
        assert other.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), SPEND_LOCK
        ).scalar_one()
        other.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), SPEND_LOCK)


def test_a_failure_saving_the_counts_does_not_replace_the_failure_in_flight(
    spend: Spend, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = spend.one_lines()[1]
    spend.transport.responses[second] = TransportResponse(200, {"price": "not a number"})

    def broken(*_: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(run_store, "merge_counts", broken)

    with pytest.raises(SchemaDriftError):
        spend.spend()

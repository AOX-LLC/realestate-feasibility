"""The estimate table and its store: one answer per candidate and date, newest wins,
and the cap counts only billed value-estimate calls of one period."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, delete, func, insert, select
from sqlalchemy.exc import IntegrityError

from feasibility.sourcing import estimate_store
from feasibility.sourcing.estimate_store import EstimateWrite
from feasibility.tables import api_request_log, candidate, candidate_estimate, sourcing_run

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
ADDRESS = "1893 THISTLEWANE DR, DALLAS, TX 75218"
COMP = {
    "address": "12 SAMPLE ST, DALLAS, TX 75218",
    "price": "1330000",
    "living_area_sqft": 3000,
    "distance_miles": "0.40",
    "year_built": 2021,
    "days_old": 45,
}


def _candidate(engine: Engine, key: str = "acct:1") -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(candidate)
            .values(market="dallas", property_key=key, street_key=key, first_as_of=DAY_ONE)
            .returning(candidate.c.id)
        ).scalar_one()


def _ok(
    candidate_id: int, fetched_on: date, price: str = "900000", **extra: object
) -> EstimateWrite:
    return EstimateWrite(
        candidate_id,
        fetched_on,
        "ok",
        ADDRESS,
        price=Decimal(price),
        price_low=Decimal("850000"),
        price_high=Decimal("950000"),
        comp_count=1,
        dropped_comp_count=2,
        comps=[COMP],
        **extra,  # type: ignore[arg-type]
    )


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(candidate_estimate)).scalar_one()


def test_saving_twice_for_one_candidate_and_date_leaves_one_row(engine: Engine) -> None:
    candidate_id = _candidate(engine)

    with engine.begin() as connection:
        estimate_store.save_estimate(connection, _ok(candidate_id, DAY_ONE, "900000"))
        estimate_store.save_estimate(connection, _ok(candidate_id, DAY_ONE, "910000"))

    assert _count(engine) == 1
    with engine.connect() as connection:
        stored = estimate_store.latest_estimates(connection, [candidate_id], DAY_ONE)[candidate_id]
    assert stored.price == Decimal("910000.00")
    assert stored.comps == [COMP]
    assert (stored.comp_count, stored.dropped_comp_count, stored.address) == (1, 2, ADDRESS)


def test_a_no_estimate_row_stores_without_a_price(engine: Engine) -> None:
    candidate_id = _candidate(engine)

    with engine.begin() as connection:
        estimate_store.save_estimate(
            connection, EstimateWrite(candidate_id, DAY_ONE, "no_estimate", ADDRESS)
        )

    with engine.connect() as connection:
        stored = estimate_store.latest_estimates(connection, [candidate_id], DAY_ONE)[candidate_id]
    assert (stored.outcome, stored.price, stored.comps) == ("no_estimate", None, [])


def test_an_ok_row_needs_a_price_and_a_no_estimate_row_cannot_have_one(engine: Engine) -> None:
    candidate_id = _candidate(engine)

    with pytest.raises(IntegrityError, match="ok_has_price"), engine.begin() as connection:
        estimate_store.save_estimate(
            connection, EstimateWrite(candidate_id, DAY_ONE, "ok", ADDRESS)
        )
    with pytest.raises(IntegrityError, match="ok_has_price"), engine.begin() as connection:
        estimate_store.save_estimate(
            connection,
            EstimateWrite(candidate_id, DAY_ONE, "no_estimate", ADDRESS, price=Decimal(1)),
        )
    with pytest.raises(IntegrityError, match="outcome"), engine.begin() as connection:
        estimate_store.save_estimate(
            connection, EstimateWrite(candidate_id, DAY_ONE, "bogus", ADDRESS, price=Decimal(1))
        )


def test_latest_estimates_returns_the_newest_row_at_or_before_the_date(engine: Engine) -> None:
    first, second = _candidate(engine, "acct:1"), _candidate(engine, "acct:2")
    with engine.begin() as connection:
        estimate_store.save_estimate(connection, _ok(first, date(2026, 9, 20), "800000"))
        estimate_store.save_estimate(connection, _ok(first, DAY_ONE, "900000"))
        estimate_store.save_estimate(connection, _ok(first, DAY_TWO, "950000"))
        estimate_store.save_estimate(connection, _ok(second, DAY_TWO, "700000"))

    with engine.connect() as connection:
        on_day_one = estimate_store.latest_estimates(connection, [first, second], DAY_ONE)
        on_day_two = estimate_store.latest_estimates(connection, [first, second], DAY_TWO)
        before_all = estimate_store.latest_estimates(connection, [first], date(2026, 9, 1))
        nobody = estimate_store.latest_estimates(connection, [], DAY_TWO)

    assert {k: v.price for k, v in on_day_one.items()} == {first: Decimal("900000.00")}
    assert {k: v.price for k, v in on_day_two.items()} == {
        first: Decimal("950000.00"),
        second: Decimal("700000.00"),
    }
    assert before_all == {} and nobody == {}


def _log(
    engine: Engine, *, endpoint: str, period: date | None, billed: bool, provider: str = "rentcast"
) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(api_request_log).values(
                provider=provider,
                endpoint=endpoint,
                request_key="k",
                period_start=period,
                outcome="ok" if billed else "cache_hit",
                billed=billed,
            )
        )


def test_billed_estimate_calls_counts_only_billed_value_calls_of_the_period(engine: Engine) -> None:
    period, other = date(2026, 10, 1), date(2026, 9, 1)
    for _ in range(3):
        _log(engine, endpoint="/avm/value", period=period, billed=True)
    _log(engine, endpoint="/avm/value", period=period, billed=False)  # cache hit or refund
    _log(engine, endpoint="/avm/value", period=None, billed=False)  # a cache hit has no period
    _log(engine, endpoint="/avm/value", period=other, billed=True)  # another period
    _log(engine, endpoint="/listings/sale", period=period, billed=True)  # the sync
    _log(engine, endpoint="/avm/value", period=period, billed=True, provider="other")

    with engine.connect() as connection:
        assert estimate_store.billed_estimate_calls(connection, period) == 3
        assert estimate_store.billed_estimate_calls(connection, other) == 1
        assert estimate_store.billed_estimate_calls(connection, date(2026, 11, 1)) == 0


def test_deleting_a_run_keeps_its_estimates(engine: Engine) -> None:
    candidate_id = _candidate(engine)
    with engine.begin() as connection:
        run_id = connection.execute(
            insert(sourcing_run)
            .values(market="dallas", as_of=DAY_ONE, status="completed", sync_status="fresh")
            .returning(sourcing_run.c.id)
        ).scalar_one()
        estimate_store.save_estimate(connection, _ok(candidate_id, DAY_ONE, run_id=run_id))
        connection.execute(delete(sourcing_run).where(sourcing_run.c.id == run_id))

    with engine.connect() as connection:
        stored = estimate_store.latest_estimates(connection, [candidate_id], DAY_ONE)[candidate_id]
    assert stored.run_id is None

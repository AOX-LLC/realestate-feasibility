"""Full sourcing runs over the committed synthetic snapshot. The expected numbers are the
ones the plan worked out by hand from the formulas."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select

from feasibility.config import DataMode, Settings
from feasibility.markets.loader import get_pack
from feasibility.snapshot.load import seed
from feasibility.sources.base import ListingQuery
from feasibility.sources.rentcast.client import (
    BudgetExhaustedError,
    Fetched,
    RentCastClient,
    RentCastError,
    SchemaDriftError,
)
from feasibility.sources.rentcast.models import SaleListing, ValueEstimate
from feasibility.sourcing import run as run_module
from feasibility.sourcing.errors import LiveDateError, NoSnapshotForDateError, RunOutOfOrderError
from feasibility.sourcing.run import SourcingResult, run_sourcing
from feasibility.tables import (
    api_budget,
    api_request_log,
    candidate,
    candidate_estimate,
    listing,
    listing_match,
    parcel,
    run_candidate,
    run_listing,
    sourcing_run,
)

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)

# (account, price, score) in rank order, from the plan.
DAY_ONE_RANKING = [
    ("051", 378000, "89.39"),
    ("004", 341000, "80.68"),
    ("052", 451000, "76.45"),
    ("002", 512000, "67.32"),
    ("006", 517000, "59.85"),
    ("005", 429000, "57.79"),
    ("012", 415000, "51.35"),
    ("007", 351000, "50.78"),
    ("001", 525000, "45.88"),
    ("003", 455000, "43.35"),
    ("013", 434000, "39.40"),
    ("009", 576000, "24.86"),
]
DAY_TWO_RANKING = [
    ("051", 378000, "89.40", "unchanged"),
    ("004", 321000, "83.28", "price_changed"),
    ("052", 451000, "76.46", "unchanged"),
    ("002", 512000, "67.32", "unchanged"),
    ("015", 430000, "61.47", "new"),
    ("006", 517000, "59.86", "unchanged"),
    ("068+069", 450000, "58.28", "new"),
    ("010", 355000, "57.03", "new"),
    ("067", 389000, "52.40", "new"),
    ("012", 399000, "51.35", "relisted"),
    ("007", 351000, "50.79", "unchanged"),
    ("001", 525000, "45.88", "unchanged"),
    ("003", 455000, "43.35", "unchanged"),
    ("008", 345000, "43.05", "new"),
    ("065", 359000, "42.48", "new"),
    ("013", 434000, "39.40", "unchanged"),
    ("009", 576000, "24.86", "unchanged"),
]


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    seed(engine, _settings())
    return engine


def _run(engine: Engine, as_of: date) -> SourcingResult:
    return run_sourcing(engine, _settings(), "dallas", as_of)


def _account(property_key: str) -> str:
    """'acct:99000000000000051' -> '051'; 'gis:SYN000067' -> '068+069' (the two-account lot)."""
    if property_key.startswith("gis:"):
        return "068+069"
    return property_key.removeprefix("acct:")[-3:]


def _ranking(engine: Engine, run_id: int) -> list[tuple[str, int, str, str]]:
    query = (
        select(
            candidate.c.property_key,
            run_listing.c.price,
            run_candidate.c.score,
            run_candidate.c.change_kind,
            run_candidate.c.rank,
        )
        .select_from(
            run_candidate.join(candidate, candidate.c.id == run_candidate.c.candidate_id).join(
                run_listing,
                (run_listing.c.run_id == run_candidate.c.run_id)
                & (run_listing.c.listing_id == run_candidate.c.primary_listing_id),
            )
        )
        .where(run_candidate.c.run_id == run_id, run_candidate.c.status == "ranked")
        .order_by(run_candidate.c.rank)
    )
    with engine.connect() as connection:
        rows = connection.execute(query).all()
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
    return [(_account(r.property_key), int(r.price), str(r.score), r.change_kind) for r in rows]


def _table_counts(engine: Engine) -> dict[str, int]:
    tables = (sourcing_run, candidate, run_listing, run_candidate, listing_match, listing)
    with engine.connect() as connection:
        return {
            table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in tables
        }


def test_day_one_counts_and_ranking(seeded: Engine) -> None:
    result = _run(seeded, DAY_ONE)
    counts = result.counts

    assert result.sync_status == "fresh"
    assert counts.listings_seen == 20
    assert counts.new == 20
    assert (counts.relisted, counts.price_changed, counts.unchanged, counts.gone) == (0, 0, 0, 0)
    assert counts.listing_filtered == 6
    assert counts.listing_filtered_by_reason.model_dump() == {
        "zip": 1,
        "property_type": 0,
        "price": 5,
    }
    assert (counts.matched, counts.ambiguous, counts.unmatched) == (14, 0, 0)
    assert counts.match_rate == "1.0000"
    assert (counts.candidates, counts.ranked, counts.filtered, counts.unscored) == (14, 12, 2, 0)

    ranking = _ranking(seeded, result.run_id)
    assert [(a, p, s) for a, p, s, _ in ranking] == DAY_ONE_RANKING


def test_the_two_mixed_parcels_fail_exactly_the_year_and_land_filters(seeded: Engine) -> None:
    result = _run(seeded, DAY_ONE)
    with seeded.connect() as connection:
        rows = connection.execute(
            select(candidate.c.property_key, run_candidate.c.filter_reasons)
            .join(candidate, candidate.c.id == run_candidate.c.candidate_id)
            .where(run_candidate.c.run_id == result.run_id, run_candidate.c.status == "filtered")
        ).all()
    assert sorted((_account(r.property_key), r.filter_reasons) for r in rows) == [
        ("041", ["year_built", "land_to_total"]),
        ("042", ["year_built", "land_to_total"]),
    ]


def test_day_two_counts_and_ranking(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)
    counts = result.counts

    assert counts.listings_seen == 27
    assert (counts.unchanged, counts.price_changed, counts.relisted, counts.new) == (17, 1, 1, 8)
    assert (counts.gone, counts.aged_out, counts.unknown_absent) == (2, 0, 0)
    assert counts.listing_filtered == 6
    assert (counts.matched, counts.ambiguous, counts.unmatched) == (19, 1, 1)
    assert counts.match_rate == "0.9048"
    assert (counts.candidates, counts.ranked, counts.filtered, counts.unscored) == (21, 17, 2, 2)

    ranking = _ranking(seeded, result.run_id)
    assert [(a, p, s, c) for a, p, s, c in ranking] == [
        (a, p, s, c) for a, p, s, c in DAY_TWO_RANKING
    ]


def test_day_two_candidate_changes_and_unscored_reasons(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)
    with seeded.connect() as connection:
        kinds = dict(
            connection.execute(
                select(run_candidate.c.change_kind, func.count())
                .where(run_candidate.c.run_id == result.run_id)
                .group_by(run_candidate.c.change_kind)
            ).all()
        )
        unscored = connection.execute(
            select(listing.c.address_line, run_candidate.c.unscored_reason)
            .join(listing, listing.c.id == run_candidate.c.primary_listing_id)
            .where(run_candidate.c.run_id == result.run_id, run_candidate.c.status == "unscored")
            .order_by(listing.c.address_line)
        ).all()
        delisted_has_candidate_row = connection.execute(
            select(run_candidate.c.candidate_id)
            .join(candidate, candidate.c.id == run_candidate.c.candidate_id)
            .where(
                run_candidate.c.run_id == result.run_id,
                candidate.c.property_key == "acct:99000000000000005",
            )
        ).all()
    assert kinds == {"new": 8, "relisted": 1, "price_changed": 1, "unchanged": 11}
    assert [(row.address_line, row.unscored_reason) for row in unscored] == [
        ("2200 KESTRELWYN DR", "ambiguous"),
        ("9100 BRINDLECOMBE ST", "unmatched"),
    ]
    assert delisted_has_candidate_row == []


def test_a_relisted_property_is_one_candidate(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    _run(seeded, DAY_TWO)
    with seeded.connect() as connection:
        rows = connection.execute(
            select(candidate.c.first_as_of).where(
                candidate.c.property_key == "acct:99000000000000012"
            )
        ).all()
    assert [row.first_as_of for row in rows] == [DAY_ONE]


def test_the_price_change_carries_the_previous_price(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)
    with seeded.connect() as connection:
        row = connection.execute(
            select(run_listing.c.price, run_listing.c.prev_price).where(
                run_listing.c.run_id == result.run_id, run_listing.c.change_kind == "price_changed"
            )
        ).one()
    assert (row.price, row.prev_price) == (Decimal(321000), Decimal(341000))


@pytest.mark.parametrize(
    ("days", "expected"),
    [([DAY_ONE, DAY_ONE], DAY_ONE_RANKING), ([DAY_ONE, DAY_TWO, DAY_TWO], DAY_TWO_RANKING)],
)
def test_rerunning_a_date_leaves_every_table_as_it_was(
    seeded: Engine, days: list[date], expected: list[tuple[Any, ...]]
) -> None:
    for day in days[:-1]:
        _run(seeded, day)
    before = _table_counts(seeded)

    again = _run(seeded, days[-1])

    assert _table_counts(seeded) == before
    ranking = _ranking(seeded, again.run_id)
    assert [(a, p, s) for a, p, s, *_ in ranking] == [(a, p, s) for a, p, s, *_ in expected]


def test_a_rerun_keeps_new_properties_new(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    _run(seeded, DAY_TWO)
    second = _run(seeded, DAY_TWO)
    assert (second.counts.new, second.counts.relisted) == (8, 1)


def test_an_earlier_date_than_the_latest_completed_run_is_refused(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    _run(seeded, DAY_TWO)
    with pytest.raises(RunOutOfOrderError):
        _run(seeded, DAY_ONE)


def test_mock_mode_needs_a_snapshot_date(seeded: Engine) -> None:
    with pytest.raises(NoSnapshotForDateError, match="2026-10-01, 2026-10-02"):
        run_sourcing(seeded, _settings(), "dallas", None)
    with pytest.raises(NoSnapshotForDateError):
        _run(seeded, date(2026, 10, 3))


def test_live_mode_refuses_a_past_date(seeded: Engine) -> None:
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key="sk-test-key"
    )
    with pytest.raises(LiveDateError):
        run_sourcing(seeded, live, "dallas", date(2020, 1, 1))


def test_constraints_hold_for_every_row_a_run_wrote(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)
    with seeded.connect() as connection:
        rows: list[Any] = connection.execute(
            select(run_candidate).where(run_candidate.c.run_id == result.run_id)
        ).all()
    ranked = [row for row in rows if row.status == "ranked"]
    assert all(row.breakdown is not None for row in ranked)
    assert sorted(row.rank for row in ranked) == list(range(1, len(ranked) + 1))
    assert all(row.breakdown is None and row.rank is None for row in rows if row.status != "ranked")


# Component points (land, age, lot, price) of the day-two new entrants, from the plan.
DAY_TWO_NEW_COMPONENTS = {
    "015": ["15.83", "20.00", "17.18", "8.46"],
    "068+069": ["22.07", "10.40", "10.00", "15.81"],
    "010": ["26.45", "5.60", "14.17", "10.81"],
    "067": ["10.83", "19.20", "10.33", "12.04"],
    "008": ["7.05", "16.00", "20.00", "0.00"],
    "065": ["13.09", "12.80", "5.33", "11.26"],
}


def test_day_two_new_entrants_have_the_documented_components(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)
    with seeded.connect() as connection:
        rows = connection.execute(
            select(candidate.c.property_key, run_candidate.c.breakdown)
            .join(candidate, candidate.c.id == run_candidate.c.candidate_id)
            .where(run_candidate.c.run_id == result.run_id, run_candidate.c.status == "ranked")
        ).all()
    breakdowns = {_account(row.property_key): row.breakdown for row in rows}

    for account, points in DAY_TWO_NEW_COMPONENTS.items():
        assert [c["points"] for c in breakdowns[account]["components"]] == points
    assert breakdowns["010"]["inputs_from_listing"] == ["lot_size"]
    assert breakdowns["015"]["match"] == {"status": "matched", "method": "stem", "account_count": 1}
    assert breakdowns["068+069"]["match"]["method"] == "gis_group"
    assert breakdowns["068+069"]["match"]["account_count"] == 2
    for breakdown in breakdowns.values():
        assert sum(Decimal(c["points"]) for c in breakdown["components"]) == Decimal(
            breakdown["total"]
        )


def _insert_listing(engine: Engine, **overrides: Any) -> None:
    seen = datetime(2026, 10, 1, 6, 0, tzinfo=ZoneInfo("America/Chicago"))
    row = {
        "source": "mls",
        "external_id": "mls-1",
        "market": "dallas",
        "address_line": "1893 THISTLEWANE DR",
        "zip5": "75218",
        "price": Decimal(339000),
        "status": "Active",
        "property_type": "Single Family",
        "raw": {},
        "first_seen_at": seen,
        "last_seen_at": seen,
        **overrides,
    }
    with engine.begin() as connection:
        connection.execute(listing.insert(), [row])


def test_two_sources_for_one_property_make_one_candidate_with_one_primary(seeded: Engine) -> None:
    baseline = _run(seeded, DAY_ONE)
    _insert_listing(seeded)
    result = _run(seeded, DAY_ONE)

    assert result.counts.candidates == baseline.counts.candidates
    assert result.counts.listings_seen == baseline.counts.listings_seen + 1
    with seeded.connect() as connection:
        rows = connection.execute(
            select(listing.c.source, run_listing.c.is_primary, run_listing.c.candidate_id)
            .join(listing, listing.c.id == run_listing.c.listing_id)
            .where(
                run_listing.c.run_id == result.run_id,
                listing.c.address_line == "1893 THISTLEWANE DR",
            )
        ).all()
    assert len(rows) == 2
    assert len({row.candidate_id for row in rows}) == 1
    assert sorted((row.source, row.is_primary) for row in rows) == [
        ("mls", True),
        ("rentcast", False),
    ]
    assert len(_ranking(seeded, result.run_id)) == 12


def test_an_unmatched_candidate_is_upgraded_in_place_when_its_parcel_arrives(
    seeded: Engine,
) -> None:
    _run(seeded, DAY_ONE)
    _run(seeded, DAY_TWO)
    street_key = "9100||BRINDLECOMBE ST"
    with seeded.connect() as connection:
        before = connection.execute(
            select(candidate.c.id, candidate.c.property_key, candidate.c.first_as_of).where(
                candidate.c.street_key == street_key
            )
        ).one()
    assert before.property_key == f"addr:75214:{street_key}"

    with seeded.begin() as connection:
        connection.execute(
            parcel.insert(),
            [
                {
                    "market": "dallas",
                    "account_id": "99000000000000999",
                    "street_number": "9100",
                    "street_name": "BRINDLECOMBE ST",
                    "zip5": "75214",
                    "land_value": Decimal(250000),
                    "improvement_value": Decimal(50000),
                    "total_value": Decimal(300000),
                    "year_built": 1948,
                    "lot_size_sqft": Decimal(7000),
                    "values_file_date": date(2026, 1, 1),
                    "attrs_file_date": date(2026, 1, 1),
                }
            ],
        )
    result = _run(seeded, DAY_TWO)

    with seeded.connect() as connection:
        after = connection.execute(
            select(candidate.c.id, candidate.c.property_key, candidate.c.account_id).where(
                candidate.c.street_key == street_key
            )
        ).all()
        status = connection.execute(
            select(run_candidate.c.status, run_candidate.c.change_kind).where(
                run_candidate.c.run_id == result.run_id, run_candidate.c.candidate_id == before.id
            )
        ).one()
    assert [(row.id, row.property_key, row.account_id) for row in after] == [
        (before.id, "acct:99000000000000999", "99000000000000999")
    ]
    assert tuple(status) == ("ranked", "new")
    assert (result.counts.unmatched, result.counts.matched) == (0, 20)


class StubClient:
    """Stands in for RentCastClient at the methods a run calls: the sync's, and the value
    estimate the spend stage buys. Both answer from the snapshot unless told to fail."""

    def __init__(
        self,
        engine: Engine,
        *,
        stale: bool = False,
        error: Exception | None = None,
        estimate_error: Exception | None = None,
    ):
        self._real = RentCastClient.from_settings(
            engine, _settings(), use_cache=False, snapshot_day="day-2"
        )
        self._stale = stale
        self._error = error
        self._estimate_error = estimate_error

    @property
    def live(self) -> bool:
        return self._real.live

    def value_estimate(self, address: str) -> Fetched[ValueEstimate | None]:
        if self._estimate_error is not None:
            raise self._estimate_error
        return self._real.value_estimate(address)

    def sale_listings(self, query: ListingQuery) -> Fetched[list[SaleListing]]:
        if self._error is not None:
            raise self._error
        fetched = self._real.sale_listings(query)
        return Fetched(fetched.data, self._stale)

    def close(self) -> None:
        self._real.close()


def _run_with(engine: Engine, stub: StubClient) -> SourcingResult:
    return run_sourcing(engine, _settings(), "dallas", DAY_TWO, client=cast(RentCastClient, stub))


def test_a_stale_sync_records_nothing_as_gone_but_still_ranks(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    result = _run_with(seeded, StubClient(seeded, stale=True))

    assert result.sync_status == "stale"
    assert (result.counts.gone, result.counts.aged_out) == (0, 0)
    assert result.counts.unknown_absent > 0
    assert result.counts.ranked > 0
    with seeded.connect() as connection:
        run = connection.execute(
            select(sourcing_run.c.status).where(sourcing_run.c.id == result.run_id)
        ).one()
    assert run.status == "completed"


def test_an_exhausted_budget_skips_the_sync_and_completes_the_run(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    error = BudgetExhaustedError(date(2026, 10, 1), 50)
    result = _run_with(seeded, StubClient(seeded, error=error))

    assert result.sync_status == "skipped"
    assert (result.counts.gone, result.counts.aged_out) == (0, 0)
    assert result.counts.unknown_absent == 20
    with seeded.connect() as connection:
        run = connection.execute(
            select(sourcing_run.c.status, sourcing_run.c.sync_status).where(
                sourcing_run.c.id == result.run_id
            )
        ).one()
    assert tuple(run) == ("completed", "skipped")


def test_any_other_sync_error_fails_the_run_and_propagates(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    error = RentCastError("/listings/sale", 500, "server_error")
    with pytest.raises(RentCastError):
        _run_with(seeded, StubClient(seeded, error=error))

    with seeded.connect() as connection:
        run = connection.execute(
            select(sourcing_run.c.status, sourcing_run.c.error).where(
                sourcing_run.c.as_of == DAY_TWO
            )
        ).one()
    assert run.status == "failed"
    assert "RentCastError" in run.error


def test_the_day_after_a_skipped_sync_is_diffed_against_the_last_fresh_run(seeded: Engine) -> None:
    """A skipped run records nothing about absence; the next run must not call every listing
    it missed relisted."""
    _run(seeded, DAY_ONE)
    error = BudgetExhaustedError(date(2026, 10, 2), 50)
    skipped = _run_with(seeded, StubClient(seeded, error=error))
    assert skipped.sync_status == "skipped"

    result = _run(seeded, date(2026, 10, 2))

    assert result.sync_status == "fresh"
    assert (result.counts.unchanged, result.counts.price_changed) == (17, 1)
    assert (result.counts.relisted, result.counts.new, result.counts.gone) == (1, 8, 2)


def test_a_failed_rerun_leaves_no_candidates_behind(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    _run(seeded, DAY_TWO)
    error = RentCastError("/listings/sale", 500, "server_error")
    with pytest.raises(RentCastError):
        _run_with(seeded, StubClient(seeded, error=error))

    with seeded.connect() as connection:
        run_id = connection.execute(
            select(sourcing_run.c.id).where(sourcing_run.c.as_of == DAY_TWO)
        ).scalar_one()
        rows = connection.execute(
            select(func.count()).select_from(run_candidate).where(run_candidate.c.run_id == run_id)
        ).scalar_one()
        listings = connection.execute(
            select(func.count()).select_from(run_listing).where(run_listing.c.run_id == run_id)
        ).scalar_one()
    assert (rows, listings) == (0, 0)


def test_an_earlier_date_is_refused_after_a_failed_later_run(seeded: Engine) -> None:
    """The failed run may already have moved last_seen_at past the earlier day."""
    _run(seeded, DAY_ONE)
    error = RentCastError("/listings/sale", 500, "server_error")
    with pytest.raises(RentCastError):
        _run_with(seeded, StubClient(seeded, error=error))

    with pytest.raises(RunOutOfOrderError):
        _run(seeded, DAY_ONE)


def test_the_build_refuses_when_a_later_run_started_after_this_one_did(seeded: Engine) -> None:
    """Two runs started together: the later date must not let the earlier one build against
    a listing table the later sync has moved on."""
    _run(seeded, DAY_ONE)
    started = run_module._start_run(seeded, "dallas", DAY_ONE)
    with seeded.begin() as connection:
        connection.execute(
            sourcing_run.insert().values(
                market="dallas", as_of=DAY_TWO, status="running", sync_status="pending"
            )
        )

    with pytest.raises(RunOutOfOrderError), seeded.begin() as connection:
        run_module._build_run(connection, get_pack("dallas"), started, DAY_ONE, "fresh")


def _run_listing_matches(engine: Engine, run_id: int) -> dict[str, tuple[str | None, ...]]:
    query = (
        select(
            listing.c.address_line,
            run_listing.c.change_kind,
            run_listing.c.match_status,
            run_listing.c.match_method,
            run_listing.c.match_account_id,
        )
        .join(listing, listing.c.id == run_listing.c.listing_id)
        .where(run_listing.c.run_id == run_id)
    )
    with engine.connect() as connection:
        return {r.address_line: tuple(r)[1:] for r in connection.execute(query)}


def test_a_run_stores_the_match_it_made_on_each_listing(seeded: Engine) -> None:
    result = _run(seeded, DAY_ONE)

    rows = _run_listing_matches(seeded, result.run_id)

    assert rows["1893 THISTLEWANE DR"] == ("new", "matched", "exact", "99000000000000004")
    assert rows["8773 ORRINMOOR TRL"] == ("new", "matched", "exact", "99000000000000051")
    # A listing that failed a listing-level filter was not matched by the run.
    assert rows["1053 VINTRELOW ST"] == ("new", None, None, None)
    statuses = [row[1] for row in rows.values()]
    assert statuses.count("matched") == result.counts.matched
    assert statuses.count(None) == len(rows) - result.counts.matched - result.counts.unmatched


def test_day_two_stores_each_match_method_and_the_gone_row_keeps_the_earlier_match(
    seeded: Engine,
) -> None:
    _run(seeded, DAY_ONE)
    result = _run(seeded, DAY_TWO)

    rows = _run_listing_matches(seeded, result.run_id)

    assert rows["3300 ORRINMOOR LN"] == ("new", "matched", "gis_group", "99000000000000068")
    assert rows["554 OSTRAVELLE AVE"] == ("new", "matched", "stem", "99000000000000015")
    assert rows["2200 KESTRELWYN DR"] == ("new", "ambiguous", None, None)
    assert rows["9100 BRINDLECOMBE ST"] == ("new", "unmatched", None, None)
    # Lost from the feed, so not re-matched: the previous run's match is carried over.
    assert rows["7059 OSTRAVELLE TRL"] == ("gone", "matched", "exact", "99000000000000005")


# --- the value-estimate spend inside a run ----------------------------------------------

DAY_ONE_PRICED = ["051", "004", "052", "002", "006"]
DAY_TWO_PRICED = ["051", "004", "052", "002", "015"]


def _estimated_accounts(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        keys = connection.execute(
            select(candidate.c.property_key)
            .join(candidate_estimate, candidate_estimate.c.candidate_id == candidate.c.id)
            .order_by(candidate.c.property_key)
        ).scalars()
        return sorted(_account(key) for key in keys)


def _estimate_requests(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(api_request_log)
            .where(api_request_log.c.endpoint == "/avm/value")
        ).scalar_one()


def _budget_rows(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(api_budget)).scalar_one()


def _stored_run(engine: Engine, run_id: int) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            select(sourcing_run.c.status, sourcing_run.c.error, sourcing_run.c.counts).where(
                sourcing_run.c.id == run_id
            )
        ).one()


def test_day_one_prices_the_top_five_and_day_two_only_the_new_entrant(seeded: Engine) -> None:
    one = _run(seeded, DAY_ONE)

    assert one.counts.estimates_targeted == 5
    assert (one.counts.estimates_called, one.counts.estimates_reused) == (5, 0)
    assert _estimated_accounts(seeded) == sorted(DAY_ONE_PRICED)
    assert _estimate_requests(seeded) == 5

    two = _run(seeded, DAY_TWO)

    # Day 2's top five are 051, 004, 052, 002 and the new 015; the first four are reused.
    assert (two.counts.estimates_called, two.counts.estimates_reused) == (1, 4)
    assert _estimated_accounts(seeded) == sorted({*DAY_ONE_PRICED, *DAY_TWO_PRICED})
    assert _estimate_requests(seeded) == 6
    stored = _stored_run(seeded, two.run_id)
    assert stored.counts["estimates_called"] == 1
    assert stored.error is None


def test_mock_mode_never_touches_the_budget_and_ignores_a_tiny_monthly_budget(
    seeded: Engine,
) -> None:
    tiny = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.MOCK, rentcast_monthly_budget=0
    )

    result = run_sourcing(seeded, tiny, "dallas", DAY_ONE)

    assert (result.counts.estimates_called, result.counts.estimates_deferred) == (5, 0)
    assert _budget_rows(seeded) == 0


def test_rerunning_a_date_buys_nothing_again(seeded: Engine) -> None:
    _run(seeded, DAY_ONE)
    before = _estimate_requests(seeded)

    again = _run(seeded, DAY_ONE)

    assert (again.counts.estimates_called, again.counts.estimates_reused) == (0, 5)
    assert _estimate_requests(seeded) == before
    assert _estimated_accounts(seeded) == sorted(DAY_ONE_PRICED)


def test_a_failed_estimate_stage_keeps_the_ranking_records_the_error_and_a_retry_finishes(
    seeded: Engine,
) -> None:
    _run(seeded, DAY_ONE)
    drift = SchemaDriftError("/avm/value", ["price"])
    with pytest.raises(SchemaDriftError):
        _run_with(seeded, StubClient(seeded, estimate_error=drift))

    with seeded.connect() as connection:
        run_id = connection.execute(
            select(sourcing_run.c.id).where(sourcing_run.c.as_of == DAY_TWO)
        ).scalar_one()
    failed = _stored_run(seeded, run_id)
    assert failed.status == "completed"
    assert failed.error.startswith("SchemaDriftError")
    assert failed.counts["estimates_failed"] == 1
    assert failed.counts["estimates_reused"] == 4
    assert len(_ranking(seeded, run_id)) == 17

    retry = _run(seeded, DAY_TWO)

    assert retry.run_id == run_id
    assert (retry.counts.estimates_called, retry.counts.estimates_reused) == (1, 4)
    assert _stored_run(seeded, run_id).error is None
    # Day one's five were never bought again.
    assert _estimate_requests(seeded) == 6

"""What the pro-forma reads from the database for a run: the property as the run saw it, the
parcel (or the accounts of a GIS group) and the newest estimate on or before the run date."""

import logging
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, delete, select, update

from feasibility.config import DataMode, Settings
from feasibility.proforma.gather import CandidateInputs, gather_inputs
from feasibility.snapshot.load import seed
from feasibility.sourcing.estimate_store import EstimateWrite, save_estimate
from feasibility.sourcing.run import SourcingResult, run_sourcing
from feasibility.tables import candidate, parcel, run_listing

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)
GROUP_ACCOUNTS = ("99000000000000068", "99000000000000069")


@pytest.fixture
def runs(engine: Engine) -> tuple[SourcingResult, SourcingResult]:
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    seed(engine, settings)
    return (
        run_sourcing(engine, settings, "dallas", DAY_ONE),
        run_sourcing(engine, settings, "dallas", DAY_TWO),
    )


def _gather(
    engine: Engine, run: SourcingResult, as_of: date | None = None
) -> list[CandidateInputs]:
    with engine.connect() as connection:
        return gather_inputs(connection, "dallas", run.run_id, as_of or run.as_of)


def _by_account(
    engine: Engine, gathered: list[CandidateInputs], account_suffix: str
) -> CandidateInputs:
    """The gathered candidate whose account id ends with the suffix (e.g. "051")."""
    with engine.connect() as connection:
        ids = {
            row.id
            for row in connection.execute(
                select(candidate.c.id).where(candidate.c.account_id.like(f"%{account_suffix}"))
            )
        }
    (found,) = [item for item in gathered if item.candidate_id in ids]
    return found


def test_every_ranked_candidate_gets_inputs_in_rank_order(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    day_one, day_two = (_gather(engine, run) for run in runs)

    assert [item.rank for item in day_one] == list(range(1, 13))
    assert [item.rank for item in day_two] == list(range(1, 18))


def test_a_gis_group_is_one_lot_with_its_accounts_living_area_added_up(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    group = _by_account(engine, _gather(engine, runs[1]), "068").inputs

    assert group.is_gis_group
    assert group.lot_sqft == Decimal("9000.00")  # one lot, counted once
    assert group.lot_source == "parcel"
    assert group.existing_living_sqft == 1800
    assert group.zoning == "R-7.5(A)"
    assert group.price == Decimal("450000.00")


def test_a_single_account_is_not_a_group(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    single = _by_account(engine, _gather(engine, runs[0]), "004").inputs

    assert not single.is_gis_group
    assert single.existing_living_sqft == 996
    assert single.lot_sqft == Decimal("12904.00")
    assert not single.is_vacant


def test_a_vacant_lot_with_a_zoning_the_pack_has_no_rule_for(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    vacant = _by_account(engine, _gather(engine, runs[0]), "052").inputs

    assert vacant.is_vacant
    assert (vacant.zoning, vacant.zoning_values_seen) == ("CD-12", ("CD-12",))
    assert vacant.existing_living_sqft is None


def test_the_price_is_the_one_the_run_recorded_not_the_listing_s_latest(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    # Acct 004 was cut from 341,000 to 321,000 on day two; day one's inputs keep the old price.
    prices = [_by_account(engine, _gather(engine, run), "004").inputs.price for run in runs]

    assert prices == [Decimal("341000.00"), Decimal("321000.00")]


def test_a_lot_the_parcel_does_not_have_comes_from_the_listing(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    inputs = _by_account(engine, _gather(engine, runs[1]), "010").inputs

    assert inputs.lot_source == "listing"
    assert inputs.lot_sqft == Decimal("10250.00")


def test_a_candidate_with_no_positive_price_in_the_run_gets_no_inputs(
    engine: Engine,
    runs: tuple[SourcingResult, SourcingResult],
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _gather(engine, runs[0])[0]
    with engine.begin() as connection:
        connection.execute(
            update(run_listing)
            .where(
                run_listing.c.run_id == runs[0].run_id,
                run_listing.c.candidate_id == first.candidate_id,
                run_listing.c.is_primary,
            )
            .values(price=Decimal(0))
        )

    with caplog.at_level(logging.WARNING):
        gathered = _gather(engine, runs[0])

    assert first.candidate_id not in {item.candidate_id for item in gathered}
    assert len(gathered) == 11
    assert str(first.candidate_id) in caplog.text


def _set_zoning(engine: Engine, account_id: str, zoning: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(parcel).where(parcel.c.account_id == account_id).values(zoning=zoning)
        )


def test_accounts_that_disagree_on_zoning_give_no_single_zoning(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    _set_zoning(engine, GROUP_ACCOUNTS[1], "R-5(A)")

    group = _by_account(engine, _gather(engine, runs[1]), "068").inputs

    assert group.zoning is None
    assert group.zoning_values_seen == ("R-5(A)", "R-7.5(A)")


@pytest.mark.parametrize("other", [None, "", "  ", "r-7.5(a)", "R-7.5 (A)"])
def test_blank_or_respelled_zoning_among_the_accounts_is_still_one_zoning(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult], other: str | None
) -> None:
    _set_zoning(engine, GROUP_ACCOUNTS[1], other)

    group = _by_account(engine, _gather(engine, runs[1]), "068").inputs

    assert group.zoning == "R-7.5(A)"
    assert group.zoning_values_seen == ("R-7.5(A)",)


def test_a_parcel_that_is_gone_leaves_the_listing_s_facts(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    with engine.begin() as connection:
        connection.execute(delete(parcel).where(parcel.c.account_id == "99000000000000004"))

    inputs = _by_account(engine, _gather(engine, runs[0]), "004").inputs

    assert inputs.lot_source == "listing"
    assert inputs.zoning is None and inputs.zoning_values_seen == ()
    assert inputs.existing_living_sqft is None


def test_the_estimate_is_the_newest_one_on_or_before_the_run_date(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    day_two = _gather(engine, runs[1])
    # Day one bought 051, 004, 052, 002 and 006; day two added 015 and reused the others.
    fetched = {
        item.rank: item.inputs.estimate.fetched_on if item.inputs.estimate else None
        for item in day_two[:6]
    }
    assert fetched == {
        1: DAY_ONE,
        2: DAY_ONE,
        3: DAY_ONE,
        4: DAY_ONE,
        5: DAY_TWO,
        6: DAY_ONE,
    }
    assert all(item.inputs.estimate is None for item in day_two[6:])


def test_an_estimate_from_after_the_run_date_is_not_used(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    four = _by_account(engine, _gather(engine, runs[0]), "004")
    with engine.begin() as connection:
        save_estimate(
            connection,
            EstimateWrite(four.candidate_id, date(2026, 10, 9), "no_estimate", "4 TEST ST"),
        )

    before = _by_account(engine, _gather(engine, runs[0]), "004").inputs.estimate
    after = _by_account(engine, _gather(engine, runs[0], date(2026, 10, 9)), "004").inputs.estimate

    assert before is not None and before.fetched_on == DAY_ONE and before.outcome == "ok"
    assert after is not None and after.fetched_on == date(2026, 10, 9)
    assert (after.outcome, after.price, after.comps) == ("no_estimate", None, ())


def test_stored_comps_become_engine_comps_without_the_fields_the_engine_does_not_take(
    engine: Engine, runs: tuple[SourcingResult, SourcingResult]
) -> None:
    four = _by_account(engine, _gather(engine, runs[0]), "004")
    comps = [
        {
            "address": "1 A ST, DALLAS, TX 75218",
            "price": "1330000.00",
            "living_area_sqft": 3000,
            "distance_miles": "0.40",
            "year_built": 2021,
            "days_old": 45,
        },
        {
            "address": "2 B ST, DALLAS, TX 75218",
            "price": "0",
            "living_area_sqft": 3000,
            "distance_miles": None,
            "year_built": None,
            "days_old": 1,
        },
    ]
    with engine.begin() as connection:
        save_estimate(
            connection,
            EstimateWrite(
                four.candidate_id,
                DAY_ONE,
                "ok",
                "4 TEST ST",
                price=Decimal("900000"),
                comp_count=2,
                comps=comps,
            ),
        )

    estimate = _by_account(engine, _gather(engine, runs[0]), "004").inputs.estimate

    assert estimate is not None
    # The comp that sold for nothing never reaches the engine.
    assert [
        (c.address, c.price, c.living_area_sqft, c.distance_miles, c.year_built)
        for c in estimate.comps
    ] == [("1 A ST, DALLAS, TX 75218", Decimal("1330000.00"), 3000, Decimal("0.40"), 2021)]
    assert not hasattr(estimate.comps[0], "days_old")

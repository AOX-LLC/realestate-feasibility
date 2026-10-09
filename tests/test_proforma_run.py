"""Pro-formas in the sourcing run, over the committed synthetic snapshot.

The margins and profits below were computed by the engine on 2026-10-08 and checked against the
bands the plan derived by hand from the formula section (section 4's table). The bands stay as a
second, looser assertion: a change to the fixtures or the pack that moves a demo pro-forma out of
its band fails loudly.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from conftest import empty_database
from sqlalchemy import Engine, func, select, update

from feasibility.config import DataMode, Settings
from feasibility.proforma import store
from feasibility.proforma.model import ProformaResult
from feasibility.proforma.store import StoredProforma
from feasibility.snapshot.load import seed
from feasibility.sourcing import run as run_module
from feasibility.sourcing.run import SourcingResult, run_sourcing
from feasibility.tables import (
    api_budget,
    candidate,
    candidate_estimate,
    proforma,
    run_candidate,
    sourcing_run,
)

DAY_ONE = date(2026, 10, 1)
DAY_TWO = date(2026, 10, 2)

# (day, account, list price, margin band, exact margin, exact profit). Section 4's table: the
# bands are the plan's, the exact values are what the engine computes from the committed fixtures.
DEMO = [
    (DAY_ONE, "051", "378000.00", ("0.181", "0.212"), "0.1964", "329563.07"),
    (DAY_ONE, "004", "341000.00", ("0.166", "0.199"), "0.1806", "289632.37"),
    (DAY_ONE, "052", "451000.00", ("0.056", "0.094"), "0.0646", "91117.19"),
    (DAY_ONE, "002", "512000.00", ("-0.064", "-0.016"), "-0.0327", "-47282.77"),
    (DAY_ONE, "006", "517000.00", ("0.017", "0.057"), "0.0394", "62121.09"),
    (DAY_TWO, "004", "321000.00", ("0.180", "0.213"), "0.1943", "311686.38"),
    (DAY_TWO, "015", "430000.00", ("0.156", "0.188"), "0.1775", "306482.59"),
]
# Profit bands from the same table, in whole thousands.
PROFIT_BANDS = {
    (DAY_ONE, "051"): (298_000, 364_000),
    (DAY_ONE, "004"): (262_000, 328_000),
    (DAY_ONE, "052"): (79_000, 137_000),
    (DAY_ONE, "002"): (-90_000, -24_000),
    (DAY_ONE, "006"): (26_000, 92_000),
    (DAY_TWO, "004"): (284_000, 350_000),
    (DAY_TWO, "015"): (263_000, 329_000),
}
DAY_ONE_COMPUTED = ["051", "004", "052", "002", "006"]
# Acct 006 is ranked sixth on day two, behind the new 015, and still has day one's estimate,
# which the engine may use for 30 days: so day two computes six, not the plan's five.
DAY_TWO_COMPUTED = ["051", "004", "052", "002", "015", "006"]


def _label(property_key: str) -> str:
    """'acct:99000000000000051' -> '051'; 'gis:SYN000067' -> '068+069' (the two-account lot)."""
    if property_key.startswith("gis:"):
        return "068+069"
    return property_key.removeprefix("acct:")[-3:]


def _settings() -> Settings:
    return Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]


@dataclass(frozen=True)
class Day:
    result: SourcingResult
    rows: dict[str, StoredProforma]  # by account label; each carries its full result

    def result_of(self, account: str) -> ProformaResult:
        stored = self.rows[account].result
        assert stored is not None
        return ProformaResult.model_validate(stored)


def _read_day(engine: Engine, result: SourcingResult) -> Day:
    with engine.connect() as connection:
        keys = {
            row.candidate_id: row.property_key
            for row in connection.execute(
                select(run_candidate.c.candidate_id, candidate.c.property_key)
                .join(candidate, candidate.c.id == run_candidate.c.candidate_id)
                .where(run_candidate.c.run_id == result.run_id)
            )
        }
        listed = store.read_proformas(connection, result.run_id, limit=100)
        rows = {
            _label(keys[item.candidate_id]): store.read_proforma(
                connection, result.run_id, item.candidate_id
            )
            for item in listed
        }
    assert all(item is not None for item in rows.values())
    return Day(result, {label: item for label, item in rows.items() if item is not None})


@pytest.fixture(scope="module")
def days(migrated_engine: Engine) -> Iterator[dict[date, Day]]:
    """Both snapshot days, run once for the read-only tests below. They read what was captured
    here, not the database, which the next test's `engine` fixture empties."""
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    results = {
        day: run_sourcing(migrated_engine, _settings(), "dallas", day) for day in (DAY_ONE, DAY_TWO)
    }
    yield {day: _read_day(migrated_engine, result) for day, result in results.items()}


def test_day_one_ranks_twelve_and_computes_the_top_five(days: dict[date, Day]) -> None:
    day = days[DAY_ONE]

    assert [item.rank for item in day.rows.values()] == list(range(1, 13))
    computed = [label for label, item in day.rows.items() if item.status == "computed"]
    assert computed == DAY_ONE_COMPUTED
    others = [item for label, item in day.rows.items() if label not in DAY_ONE_COMPUTED]
    assert len(others) == 7
    assert {(item.status, item.reason) for item in others} == {("no_arv", "no_estimate_yet")}


def test_day_two_adds_the_new_listing_and_keeps_an_estimate_still_good_for_the_sixth(
    days: dict[date, Day],
) -> None:
    day = days[DAY_TWO]

    assert [item.rank for item in day.rows.values()] == list(range(1, 18))
    computed = [label for label, item in day.rows.items() if item.status == "computed"]
    assert computed == DAY_TWO_COMPUTED
    # 006 fell to sixth place, but day one's estimate is 1 day old and priced it then: the
    # pro-forma is the one day one computed, the same figures to the cent.
    assert day.rows["006"].rank == 6
    assert day.rows["006"].estimate_fetched_on == DAY_ONE
    one = days[DAY_ONE].rows["006"]
    assert (day.rows["006"].arv, day.rows["006"].profit) == (one.arv, one.profit)
    other = [item for label, item in day.rows.items() if label not in DAY_TWO_COMPUTED]
    assert {(item.status, item.reason) for item in other} == {("no_arv", "no_estimate_yet")}
    assert len(other) == 11


def test_the_two_account_lot_outside_the_estimate_line_has_no_arv_and_is_flagged(
    days: dict[date, Day],
) -> None:
    group = days[DAY_TWO].rows["068+069"]
    result = days[DAY_TWO].result_of("068+069")

    assert group.rank == 7
    assert (group.status, group.reason) == ("no_arv", "no_estimate_yet")
    assert "gis_group" in group.flags
    assert result.site.is_gis_group and result.site.existing_living_sqft == 1800
    assert result.site.lot_sqft == Decimal("9000.00")
    # The costs that need no ARV are still computed, and stored.
    assert result.costs is not None and result.financing is not None and result.holding is not None
    assert (result.selling, result.totals, result.max_offer, result.sensitivity) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(("day", "account", "price", "band", "margin", "profit"), DEMO)
def test_each_demo_pro_forma_is_in_its_band_and_has_its_exact_figures(
    days: dict[date, Day],
    day: date,
    account: str,
    price: str,
    band: tuple[str, str],
    margin: str,
    profit: str,
) -> None:
    row = days[day].rows[account]

    assert row.status == "computed"
    assert row.offer_price == Decimal(price)
    assert row.margin is not None and row.profit is not None
    assert Decimal(band[0]) <= row.margin <= Decimal(band[1])
    low, high = PROFIT_BANDS[(day, account)]
    assert low <= row.profit <= high
    assert (row.margin, row.profit) == (Decimal(margin), Decimal(profit))


def test_day_one_spreads_from_clearing_the_target_to_losing_money(days: dict[date, Day]) -> None:
    margins = [row.margin for row in days[DAY_ONE].rows.values() if row.margin is not None]

    assert sum(1 for margin in margins if margin >= Decimal("0.15")) >= 2
    assert sum(1 for margin in margins if Decimal(0) < margin < Decimal("0.15")) >= 1
    assert sum(1 for margin in margins if margin < 0) >= 1


def test_the_demo_pro_formas_carry_the_figures_the_plan_lists(days: dict[date, Day]) -> None:
    one, two = days[DAY_ONE], days[DAY_TWO]

    vacant = one.result_of("051")
    assert "vacant_lot" in vacant.flags
    assert vacant.costs is not None and vacant.costs.demolition == 0
    assert vacant.sizing is not None
    assert (vacant.sizing.buildable_sqft, vacant.sizing.capped_by) == (3500, "max")

    for account, demolition, day in (
        ("004", "10468.00", one),
        ("002", "9404.00", one),
        ("006", "19060.00", one),
        ("015", "10452.00", two),
    ):
        result = day.result_of(account)
        assert result.costs is not None
        assert result.costs.demolition == Decimal(demolition), account
        assert result.sizing is not None and result.sizing.buildable_sqft == 3500, account

    assumed = one.result_of("052")
    assert {"vacant_lot", "zoning_rule_assumed"} <= set(assumed.flags)
    assert assumed.site.rule_used == "default"
    assert assumed.sizing is not None and assumed.sizing.buildable_sqft == 3105

    loser = one.result_of("002")
    assert loser.max_offer is not None and loser.max_offer.max_offer is not None
    assert loser.max_offer.max_offer < Decimal("512000")


def test_a_price_cut_moves_the_pro_forma_but_not_its_arv(days: dict[date, Day]) -> None:
    before, after = days[DAY_ONE].rows["004"], days[DAY_TWO].rows["004"]

    assert (before.offer_price, after.offer_price) == (Decimal("341000.00"), Decimal("321000.00"))
    assert before.arv == after.arv
    assert before.estimate_fetched_on == after.estimate_fetched_on == DAY_ONE
    assert before.profit is not None and after.profit is not None
    assert after.profit > before.profit
    # About twenty thousand of price, plus what financing and closing cost on it.
    assert Decimal("20000") < after.profit - before.profit < Decimal("25000")


@pytest.mark.parametrize("day", [DAY_ONE, DAY_TWO])
def test_every_stored_result_revalidates_and_adds_up(days: dict[date, Day], day: date) -> None:
    for account, row in days[day].rows.items():
        result = days[day].result_of(account)
        assert row.result is not None
        assert result.model_dump(mode="json") == row.result
        assert (result.status, result.reason, list(result.flags)) == (
            row.status,
            row.reason,
            row.flags,
        )
        if row.status != "computed":
            continue
        assert result.costs and result.financing and result.holding and result.selling
        assert result.totals and result.arv
        lines = (
            result.costs.offer_price
            + result.costs.acquisition_closing
            + result.costs.demolition
            + result.costs.hard_cost
            + result.costs.contingency
            + result.costs.soft_costs
            + result.financing.total
            + result.holding.total
            + result.selling.total
        )
        assert lines == result.totals.total_cost, account
        assert result.totals.profit == result.arv.arv - result.totals.total_cost, account
        assert (row.arv, row.total_cost, row.profit, row.margin) == (
            result.arv.arv,
            result.totals.total_cost,
            result.totals.profit,
            result.totals.margin,
        )


def test_the_run_counts_its_pro_formas(days: dict[date, Day]) -> None:
    for day, computed, total in ((DAY_ONE, 5, 12), (DAY_TWO, 6, 17)):
        counts = days[day].result.counts
        assert (counts.proformas, counts.proformas_computed) == (total, computed)
        assert counts.proformas_no_arv == total - computed
        assert counts.proformas_unsizable == 0
        assert counts.proformas == counts.ranked


def test_no_pro_forma_spent_anything(days: dict[date, Day]) -> None:
    # Day one bought five estimates and day two one; the pro-forma stage added no call.
    assert days[DAY_ONE].result.counts.estimates_called == 5
    assert days[DAY_TWO].result.counts.estimates_called == 1


# --- tests that change the database -------------------------------------------------------------


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    seed(engine, _settings())
    return engine


def _count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def _ranked_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(run_candidate)
            .where(run_candidate.c.status == "ranked")
        ).scalar_one()


def _stored_run(engine: Engine, run_id: int) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            select(sourcing_run.c.status, sourcing_run.c.error, sourcing_run.c.counts).where(
                sourcing_run.c.id == run_id
            )
        ).one()


def _proforma_rows(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return connection.execute(
            select(*proforma.c).order_by(proforma.c.run_id, proforma.c.candidate_id)
        ).all()  # type: ignore[return-value]


def test_a_day_two_re_run_rewrites_the_same_rows_and_buys_nothing(seeded: Engine) -> None:
    run_sourcing(seeded, _settings(), "dallas", DAY_ONE)
    run_sourcing(seeded, _settings(), "dallas", DAY_TWO)
    before = _proforma_rows(seeded)
    sizes = [_count(seeded, table) for table in (proforma, candidate_estimate, api_budget)]

    again = run_sourcing(seeded, _settings(), "dallas", DAY_TWO)

    assert (len(before), sizes) == (29, [29, 6, 0])
    assert [_count(seeded, table) for table in (proforma, candidate_estimate, api_budget)] == sizes
    assert _proforma_rows(seeded) == before  # the same rows, figure for figure
    assert again.counts.estimates_called == 0
    assert (again.counts.proformas, again.counts.proformas_computed) == (17, 6)


def test_mock_mode_leaves_the_budget_alone(seeded: Engine) -> None:
    run_sourcing(seeded, _settings(), "dallas", DAY_ONE)

    with seeded.connect() as connection:
        used = connection.execute(
            select(func.coalesce(func.sum(api_budget.c.used), 0))
        ).scalar_one()
    assert used == 0


def test_a_failed_pro_forma_stage_keeps_the_ranking_and_the_estimates_and_a_retry_finishes(
    seeded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the engine fell over")

    with monkeypatch.context() as patch:
        patch.setattr(run_module, "run_proformas", broken)
        with pytest.raises(RuntimeError, match="fell over"):
            run_sourcing(seeded, _settings(), "dallas", DAY_ONE)

    with seeded.connect() as connection:
        run_id = connection.execute(select(sourcing_run.c.id)).scalar_one()
    failed = _stored_run(seeded, run_id)
    assert failed.status == "completed"
    assert failed.error.startswith("RuntimeError: the engine fell over")
    assert failed.counts["estimates_called"] == 5  # the paid answers were committed
    assert _ranked_count(seeded) == 12  # the ranking is intact
    assert _count(seeded, candidate_estimate) == 5
    assert _count(seeded, proforma) == 0

    retry = run_sourcing(seeded, _settings(), "dallas", DAY_ONE)

    assert retry.run_id == run_id
    assert (retry.counts.estimates_called, retry.counts.estimates_reused) == (0, 5)
    assert (retry.counts.proformas, retry.counts.proformas_computed) == (12, 5)
    assert _stored_run(seeded, run_id).error is None
    assert _count(seeded, proforma) == 12


@pytest.mark.parametrize(
    ("price", "area"),
    [
        (Decimal("1"), 4000),  # an ARV of under a dollar: a margin of minus a million
        (Decimal("999999999999999"), 600),  # 15 digits a comp, the most the engine accepts
    ],
)
def test_comps_priced_absurdly_still_store_and_do_not_fail_the_run(
    seeded: Engine, price: Decimal, area: int
) -> None:
    run_sourcing(seeded, _settings(), "dallas", DAY_ONE)
    junk = [
        {
            "address": f"{n} JUNK ST, DALLAS, TX 75209",
            "price": str(price),
            "living_area_sqft": area,
            "distance_miles": None,
            "year_built": None,
            "days_old": 1,
        }
        for n in range(5)
    ]
    with seeded.begin() as connection:
        connection.execute(update(candidate_estimate).values(comps=junk))

    again = run_sourcing(seeded, _settings(), "dallas", DAY_ONE)

    assert (again.counts.proformas, again.counts.proformas_computed) == (12, 5)
    assert _stored_run(seeded, again.run_id).error is None

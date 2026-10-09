"""Writing and reading stored pro-formas: a run's rows replace the last attempt's, list in
rank order by keyset, and carry the full result only when one is asked for."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from feasibility.config import DataMode, Settings
from feasibility.proforma import store
from feasibility.proforma.store import ProformaWrite
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing
from feasibility.tables import proforma, run_candidate

DAY_ONE = date(2026, 10, 1)


def _write(candidate_id: int, status: str = "no_arv", **fields: Any) -> ProformaWrite:
    values: dict[str, Any] = {
        "candidate_id": candidate_id,
        "status": status,
        "reason": None if status == "computed" else "no_estimate_yet",
        "estimate_fetched_on": None,
        "offer_price": Decimal("400000.00"),
        "arv": None,
        "total_cost": None,
        "profit": None,
        "margin": None,
        "roi": None,
        "annualized_return": None,
        "max_offer": None,
        "flags": ["vacant_lot"],
        "result": {"version": 1, "status": status},
    }
    values.update(fields)
    return ProformaWrite(**values)


def _computed(candidate_id: int) -> ProformaWrite:
    return _write(
        candidate_id,
        "computed",
        estimate_fetched_on=DAY_ONE,
        arv=Decimal("1000000.00"),
        total_cost=Decimal("900000.00"),
        profit=Decimal("100000.00"),
        margin=Decimal("0.1000"),
        roi=Decimal("0.3000"),
        annualized_return=Decimal("0.4000"),
        max_offer=Decimal("380000.00"),
    )


@pytest.fixture
def run(engine: Engine) -> tuple[SourcingResult, list[int]]:
    """A ranked run and its candidates' ids in rank order."""
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    seed(engine, settings)
    result = run_sourcing(engine, settings, "dallas", DAY_ONE)
    with engine.connect() as connection:
        ids = list(
            connection.execute(
                select(run_candidate.c.candidate_id)
                .where(run_candidate.c.run_id == result.run_id, run_candidate.c.status == "ranked")
                .order_by(run_candidate.c.rank)
            ).scalars()
        )
    return result, ids


def _count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(proforma)).scalar_one()


def test_a_run_s_rows_are_stored_and_listed_in_rank_order(
    engine: Engine, run: tuple[SourcingResult, list[int]]
) -> None:
    result, ids = run
    with engine.begin() as connection:
        # written out of rank order on purpose
        store.write_proformas(
            connection, result.run_id, [_write(ids[2]), _computed(ids[0]), _write(ids[1])]
        )

    with engine.connect() as connection:
        listed = store.read_proformas(connection, result.run_id, limit=10)

    assert [item.candidate_id for item in listed] == ids[:3]
    assert [item.rank for item in listed] == [1, 2, 3]
    first = listed[0]
    assert (first.status, first.reason, first.flags) == ("computed", None, ["vacant_lot"])
    assert first.arv == Decimal("1000000.00") and first.margin == Decimal("0.1000")
    assert first.street and first.zip5
    assert first.result is None  # a list does not carry every result


def test_writing_again_replaces_the_run_s_rows(
    engine: Engine, run: tuple[SourcingResult, list[int]]
) -> None:
    result, ids = run
    with engine.begin() as connection:
        store.write_proformas(connection, result.run_id, [_write(ids[0]), _write(ids[1])])
        store.write_proformas(connection, result.run_id, [_computed(ids[0])])

    with engine.connect() as connection:
        listed = store.read_proformas(connection, result.run_id, limit=10)

    assert [(item.candidate_id, item.status) for item in listed] == [(ids[0], "computed")]


def test_writing_nothing_clears_the_run(
    engine: Engine, run: tuple[SourcingResult, list[int]]
) -> None:
    result, ids = run
    with engine.begin() as connection:
        store.write_proformas(connection, result.run_id, [_write(ids[0])])
        store.write_proformas(connection, result.run_id, [])

    assert _count(engine) == 0


def test_the_list_filters_by_status_and_walks_by_rank(
    engine: Engine, run: tuple[SourcingResult, list[int]]
) -> None:
    result, ids = run
    rows = [_computed(ids[0]), _write(ids[1]), _computed(ids[2]), _write(ids[3]), _write(ids[4])]
    with engine.begin() as connection:
        store.write_proformas(connection, result.run_id, rows)

    with engine.connect() as connection:
        computed = store.read_proformas(connection, result.run_id, status="computed", limit=10)
        after_one = store.read_proformas(connection, result.run_id, after_rank=1, limit=2)
        after_last = store.read_proformas(connection, result.run_id, after_rank=5, limit=2)

    assert [item.rank for item in computed] == [1, 3]
    assert [item.rank for item in after_one] == [2, 3]
    assert after_last == []


def test_one_proforma_comes_with_its_whole_result(
    engine: Engine, run: tuple[SourcingResult, list[int]]
) -> None:
    result, ids = run
    with engine.begin() as connection:
        store.write_proformas(connection, result.run_id, [_computed(ids[0])])

    with engine.connect() as connection:
        found = store.read_proforma(connection, result.run_id, ids[0])
        missing = store.read_proforma(connection, result.run_id, ids[1])
        other_run = store.read_proforma(connection, result.run_id + 1, ids[0])

    assert found is not None and found.result == {"version": 1, "status": "computed"}
    assert found.estimate_fetched_on == DAY_ONE
    assert missing is None and other_run is None

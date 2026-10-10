"""The cost assumptions a stored pro-forma keeps are frozen: the market pack can change without
touching what a stored result says or whether it can be read."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brief_support import DAY_ONE, DAY_TWO
from conftest import empty_database
from retention_support import run_sql
from sqlalchemy import Engine
from test_api import _settings

from feasibility.markets.loader import get_pack
from feasibility.markets.schema import CostAssumptions
from feasibility.proforma.assumptions_v1 import CostAssumptionsV1
from feasibility.proforma.engine import frozen_assumptions
from feasibility.proforma.model import ProformaResult
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import run_sourcing

FIXTURE = Path(__file__).parent / "fixtures" / "proforma" / "results_before_v1.json"


def pack_assumptions() -> CostAssumptions:
    return get_pack("dallas").cost_assumptions


def test_the_dallas_pack_round_trips_into_the_frozen_model_unchanged() -> None:
    pack = pack_assumptions()

    frozen = frozen_assumptions(pack)

    assert isinstance(frozen, CostAssumptionsV1)
    assert frozen.model_dump(mode="json") == pack.model_dump(mode="json")
    assert frozen.model_dump() == pack.model_dump()  # and the decimals are the same decimals


def test_a_result_is_typed_with_the_frozen_model_and_not_the_packs() -> None:
    annotation = ProformaResult.model_fields["assumptions"].annotation

    assert annotation is CostAssumptionsV1
    assert not issubclass(CostAssumptionsV1, CostAssumptions)
    # None of the frozen classes is a subclass of a pack class: changing the pack cannot reach them.
    from feasibility.markets import schema as pack_schema
    from feasibility.proforma import assumptions_v1

    pack_classes = {
        c
        for c in vars(pack_schema).values()
        if isinstance(c, type) and c.__module__ == pack_schema.__name__
    }
    for frozen in vars(assumptions_v1).values():
        if isinstance(frozen, type) and frozen.__module__ == assumptions_v1.__name__:
            assert not set(frozen.__mro__) & pack_classes, frozen


def test_reading_a_stored_result_never_goes_through_the_packs_own_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the read path used the pack's class, a new required field in the pack would break it.
    Making that class refuse to validate anything stands in for that change."""
    stored: dict[str, Any] = next(iter(json.loads(FIXTURE.read_text())["2026-10-01"].values()))

    def refuses(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a stored result was read through the pack's model")

    monkeypatch.setattr(CostAssumptions, "model_validate", refuses)
    monkeypatch.setattr(CostAssumptions, "__init__", refuses)

    read = ProformaResult.model_validate(stored)

    assert read.assumptions.status == stored["assumptions"]["status"]
    assert isinstance(read.assumptions, CostAssumptionsV1)


def test_a_stored_result_with_an_unknown_assumption_is_still_refused() -> None:
    stored = json.loads(
        json.dumps(next(iter(json.loads(FIXTURE.read_text())["2026-10-01"].values())))
    )
    stored["assumptions"]["invented"] = 1

    with pytest.raises(ValueError, match="invented"):
        ProformaResult.model_validate(stored)


def test_day_one_and_day_two_write_exactly_the_results_they_wrote_before_the_change(
    migrated_engine: Engine,
) -> None:
    """`results_before_v1.json` was captured from the code before the assumptions were frozen."""
    engine = migrated_engine
    before = json.loads(FIXTURE.read_text())
    empty_database(engine)
    seed(engine, _settings())
    try:
        for day in (DAY_ONE, DAY_TWO):
            run_sourcing(engine, _settings(), "dallas", day)
            rows = run_sql(
                engine,
                "SELECT c.property_key, p.result FROM proforma p "
                "JOIN candidate c ON c.id = p.candidate_id "
                "JOIN sourcing_run r ON r.id = p.run_id WHERE r.as_of = :d "
                "ORDER BY c.property_key",
                d=day,
            ).all()
            after = {key: result for key, result in rows}
            expected = before[day.isoformat()]

            assert set(after) == set(expected)
            for key in expected:
                assert json.dumps(after[key], sort_keys=True) == json.dumps(
                    expected[key], sort_keys=True
                ), (day, key)
    finally:
        empty_database(engine)


def test_a_changed_pack_value_still_changes_the_result_it_produces() -> None:
    """Freezing the shape must not freeze the numbers: a new hard cost is a new assumption."""
    pack = pack_assumptions()
    changed = CostAssumptions.model_validate(
        {
            **pack.model_dump(),
            "construction": {
                **pack.construction.model_dump(),
                "hard_cost_per_sqft": pack.construction.hard_cost_per_sqft + Decimal(10),
            },
        }
    )

    assert (
        frozen_assumptions(changed).construction.hard_cost_per_sqft
        == pack.construction.hard_cost_per_sqft + 10
    )

"""The brief: built by code from a run's rows, carrying only what may be delivered."""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from brief_support import (
    DAY_ONE,
    DAY_TWO,
    FREE,
    built,
    keys_matching,
    quoting_model,
    rows,
)
from conftest import empty_database
from llm_fakes import RunModel
from sqlalchemy import Engine, text
from test_api import _settings

from feasibility.delivery import store
from feasibility.delivery.brief import (
    FOOTER,
    MAX_CANDIDATES,
    NARRATIVE_LABEL,
    NOTE_NOT_AVAILABLE,
    NOTE_RECHECK_FAILED,
    NOTE_REJECTED,
    Brief,
)
from feasibility.delivery.build import BriefNotReadyError, RunNotFoundError
from feasibility.jobs.handlers import PERMANENT_ERRORS
from feasibility.llm.narrative_check import NarrativeDraft
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing


@pytest.fixture(scope="module")
def days(migrated_engine: Engine) -> Iterator[tuple[Engine, SourcingResult, SourcingResult]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = quoting_model()
    one = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE, model=model)
    two = run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO, model=model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    return migrated_engine


# --- day one and day two ------------------------------------------------------------------------


def test_day_one_has_five_candidates_in_rank_order(days: Any) -> None:
    engine, one, _ = days

    brief = built(engine, one.run_id)

    assert brief.version == 1
    assert (brief.market, brief.as_of, brief.run_id) == ("dallas", DAY_ONE, one.run_id)
    assert (brief.completeness, brief.notice, brief.data_mode) == ("complete", None, "mock")
    assert (brief.ranked, brief.shown) == (12, 5)
    assert brief.not_shown.model_dump() == {"no_arv": 7, "unsizable": 0, "over_the_cap": 0}
    ranks = [entry.rank for entry in brief.candidates]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 5
    assert brief.footer == FOOTER


def test_day_two_has_six(days: Any) -> None:
    engine, _, two = days

    brief = built(engine, two.run_id)

    assert (brief.ranked, brief.shown) == (17, 6)
    assert brief.not_shown.no_arv == 11


def test_every_figure_equals_the_stored_pro_forma_column(days: Any) -> None:
    engine, one, _ = days
    brief = built(engine, one.run_id)
    stored = {
        row.candidate_id: row
        for row in rows(
            engine,
            "SELECT candidate_id, offer_price, arv, total_cost, profit, margin, roi, "
            "annualized_return, max_offer FROM proforma "
            "WHERE run_id = :run AND status = 'computed'",
            run=one.run_id,
        )
    }

    assert set(stored) == {entry.candidate_id for entry in brief.candidates}
    for entry in brief.candidates:
        row, figures = stored[entry.candidate_id], entry.figures
        assert Decimal(figures.offer_price) == row.offer_price
        assert Decimal(figures.arv) == row.arv
        assert Decimal(figures.total_cost) == row.total_cost
        assert Decimal(figures.profit) == row.profit
        assert Decimal(figures.margin) == row.margin
        assert Decimal(figures.max_offer) == row.max_offer if figures.max_offer else True
        assert Decimal(entry.list_price) == row.offer_price


def test_the_comps_are_a_count_a_median_and_a_price_range(days: Any) -> None:
    engine, one, _ = days
    brief = built(engine, one.run_id)

    for entry in brief.candidates:
        result = rows(
            engine,
            "SELECT result FROM proforma WHERE run_id = :run AND candidate_id = :c",
            run=one.run_id,
            c=entry.candidate_id,
        )[0].result
        used = [Decimal(c["price"]) for c in result["arv"]["comps"] if c["used"]]
        assert entry.comps.count_used == result["arv"]["comp_count_used"] == len(used)
        assert Decimal(entry.comps.price_low or "0") == min(used)
        assert Decimal(entry.comps.price_high or "0") == max(used)
        assert Decimal(entry.comps.median_psf or "0") == Decimal(result["arv"]["median_psf"])


def test_the_verdict_and_flags_are_code_facts(days: Any) -> None:
    engine, one, _ = days

    for entry in built(engine, one.run_id).candidates:
        assert entry.verdict
        assert set(entry.verdict) <= {
            "clears_target",
            "below_target",
            "negative_profit",
            "no_viable_offer",
            "max_offer_below_price",
            "max_offer_above_price",
        }
        assert all(flag.meaning for flag in entry.flags)


def test_signals_carry_code_polarity_source_and_meaning_and_no_quote(days: Any) -> None:
    engine, _, two = days
    quotes = [
        signal["quote"]
        for row in rows(
            engine, "SELECT result FROM candidate_signals WHERE run_id = :run", run=two.run_id
        )
        for signal in row.result["signals"]
        if signal.get("quote")
    ]
    assert quotes  # the scripted model did quote remarks

    brief = built(engine, two.run_id)
    dumped = brief.model_dump_json()

    assert any(entry.signals.items for entry in brief.candidates)
    for quote in quotes:
        assert quote not in dumped
    for entry in brief.candidates:
        for signal in entry.signals.items:
            assert signal.meaning and signal.source in ("remarks", "fields")


def test_a_walk_of_the_brief_finds_no_address_remarks_or_quote_and_no_name_trap_key(
    days: Any,
) -> None:
    engine, one, two = days
    for run in (one, two):
        brief = built(engine, run.run_id)
        document = brief.model_dump(mode="json")
        text_of_brief = json.dumps(document)

        addresses = {
            comp["address"]
            for row in rows(
                engine, "SELECT result FROM proforma WHERE run_id = :run", run=run.run_id
            )
            for comp in (row.result["arv"] or {"comps": []})["comps"]
        }
        assert addresses
        assert not any(address in text_of_brief for address in addresses)
        for remarks in (
            r.remarks for r in rows(engine, "SELECT remarks FROM listing") if r.remarks
        ):
            windows = {remarks[i : i + 24] for i in range(0, max(1, len(remarks) - 23), 8)}
            assert [w for w in windows if w.strip() and w in text_of_brief] == []
        assert not keys_matching(document)


def test_an_accepted_narrative_is_labelled_and_carries_its_text(days: Any) -> None:
    engine, one, _ = days

    entry = built(engine, one.run_id).candidates[0]

    assert entry.narrative.status == "accepted"
    assert entry.narrative.note == NARRATIVE_LABEL
    assert entry.narrative.summary == "A plain summary of where this candidate stands."


def test_two_builds_and_a_same_day_rerun_hash_the_same(days: Any) -> None:
    engine, _, two = days
    first = built(engine, two.run_id).content_sha256()
    second = built(engine, two.run_id).content_sha256()

    rerun = run_sourcing(engine, _settings(), "dallas", DAY_TWO, model=quoting_model())

    assert first == second == built(engine, rerun.run_id).content_sha256()


def test_the_stored_brief_is_replaced_in_place_and_read_back(days: Any) -> None:
    engine, one, _ = days
    brief = built(engine, one.run_id)

    with engine.begin() as connection:
        first = store.write_brief(connection, brief)
        second = store.write_brief(connection, brief)
    with engine.connect() as connection:
        stored = store.read_brief(connection, one.run_id)

    assert first == second == brief.content_sha256()
    assert stored is not None
    assert stored.content_sha256 == first
    assert Brief.model_validate(stored.content) == brief
    assert rows(engine, "SELECT count(*) FROM brief WHERE run_id = :r", r=one.run_id)[0][0] == 1


# --- when a narrative may not be delivered ------------------------------------------------------


def test_a_rejected_narrative_has_no_text_and_a_fixed_note(seeded: Engine) -> None:
    model = RunModel(
        draft=lambda facts, feedback: NarrativeDraft(
            summary="Profit could reach $1 million.", risks=[], checks_before_offer=[]
        ),
        **FREE,
    )
    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=model)

    brief = built(seeded, result.run_id)

    assert result.counts.narratives_rejected == 5
    for entry in brief.candidates:
        assert entry.narrative.status == "withheld"
        assert entry.narrative.note == NOTE_REJECTED
        assert entry.narrative.summary is None
        assert entry.narrative.risks == []
    dumped = brief.model_dump_json()
    assert "million" not in dumped
    assert "unlisted_figure" not in dumped


def test_an_accepted_summary_edited_to_hold_a_figure_is_withheld(seeded: Engine) -> None:
    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=RunModel(**FREE))
    with seeded.begin() as connection:
        connection.execute(
            text(
                "UPDATE candidate_narrative SET result = jsonb_set(result, '{summary}', "
                "'\"The profit is $1.\"') WHERE run_id = :run AND status = 'accepted'"
            ),
            {"run": result.run_id},
        )

    brief = built(seeded, result.run_id)

    assert {e.narrative.status for e in brief.candidates} == {"withheld"}
    assert {e.narrative.note for e in brief.candidates} == {NOTE_RECHECK_FAILED}
    assert "$1." not in brief.model_dump_json()


def test_deferred_and_failed_narratives_are_not_available(seeded: Engine) -> None:
    settings = _settings().model_copy(update={"llm_run_budget_usd": Decimal("0.01")})
    result = run_sourcing(seeded, settings, "dallas", DAY_ONE, model=RunModel(**FREE))

    brief = built(seeded, result.run_id)

    assert result.counts.narratives_deferred == 5
    assert {e.narrative.status for e in brief.candidates} == {"not_available"}
    assert {e.narrative.note for e in brief.candidates} == {NOTE_NOT_AVAILABLE}
    assert brief.completeness == "complete"


# --- a run whose later stage failed, and runs that cannot be briefed ----------------------------


def test_a_run_with_an_error_is_partial_and_the_error_is_never_copied(seeded: Engine) -> None:
    result = run_sourcing(seeded, _settings(), "dallas", DAY_ONE, model=RunModel(**FREE))
    with seeded.begin() as connection:
        connection.execute(
            text("UPDATE sourcing_run SET error = 'PermanentModelError: CANARY-ERROR-TEXT'"),
        )

    brief = built(seeded, result.run_id)

    assert (brief.completeness, brief.notice) == ("partial", "later_stage_failed")
    assert "CANARY-ERROR-TEXT" not in brief.model_dump_json()
    assert brief.shown == 5


def test_a_run_that_is_still_running_is_not_ready(seeded: Engine) -> None:
    with seeded.begin() as connection:
        run_id = connection.execute(
            text(
                "INSERT INTO sourcing_run (market, as_of, status, sync_status) "
                "VALUES ('dallas', :d, 'running', 'fresh') RETURNING id"
            ),
            {"d": DAY_ONE},
        ).scalar_one()

    with pytest.raises(BriefNotReadyError):
        built(seeded, run_id)


def test_a_missing_run_is_an_error(seeded: Engine) -> None:
    with pytest.raises(RunNotFoundError):
        built(seeded, 987654)


def test_both_errors_are_permanent_for_a_job() -> None:
    assert BriefNotReadyError in PERMANENT_ERRORS or any(
        issubclass(BriefNotReadyError, kind) for kind in PERMANENT_ERRORS
    )


def test_the_cap_is_ten_and_the_rest_are_counted() -> None:
    assert MAX_CANDIDATES == 10


# --- the text rules for a delivered narrative, on their own ----------------------------


def test_unsafe_text_is_an_allowlist_so_lookalikes_and_invisible_characters_are_out() -> None:
    from feasibility.delivery.brief import TextContext, unsafe_text

    lookalike_open, lookalike_close = chr(0xFF1C), chr(0xFF1E)
    hostile = [
        f"{lookalike_open}!channel{lookalike_close} look",
        "zero" + chr(0x200B) + "width",
        "bidi" + chr(0x202E) + "override",
        "tab\there",
        "new\nline",
        "caf" + chr(0xE9),
    ]
    for said in hostile:
        assert unsafe_text([said], TextContext()), said
    assert (
        unsafe_text(["The margin clears the target, with $45,000.00 of headroom."], TextContext())
        == []
    )


def test_a_short_street_name_of_a_comp_is_caught_and_the_city_is_not() -> None:
    from feasibility.delivery.brief import TextContext, street_names_of, unsafe_text

    names = street_names_of(["12 OAK AVE, DALLAS, TX 75209", "4377 QUENDERBY LN, DALLAS, TX 75209"])
    context = TextContext(street_names=names)

    assert "quenderby" in names
    assert "dallas" not in names  # the city is not the street
    assert "oak" not in names  # a street name of three letters or fewer is a known gap
    assert unsafe_text(["A sale on Quenderby supports it."], context) == ["a street name"]
    assert unsafe_text(["Dallas buyers pay more for new homes."], context) == []

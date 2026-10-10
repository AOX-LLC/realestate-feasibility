"""Attack tests, category (b): every number that is delivered traces to a stored value.

The brief is the one delivered surface that exists in 5a; 5b's PDF, Slack and Notion tests are
marked `xfail(strict=True)` and the session removes each mark when it makes the test pass.
"""

import contextlib
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from attack_support import (
    DAY_ONE,
    DAY_TWO,
    FREE,
    build_with,
    execute,
    narrative_rows,
    rows,
    run_day,
    tamper_narratives,
)
from conftest import empty_database
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from sqlalchemy import Engine, text
from test_api import READ_HEADERS, _settings

from feasibility.api.app import create_app
from feasibility.delivery import store
from feasibility.delivery.brief import Brief
from feasibility.delivery.build import BriefError, BriefNotReadyError, build_brief
from feasibility.llm import figures
from feasibility.llm.facts import build_facts
from feasibility.proforma.model import ProformaResult
from feasibility.snapshot.load import seed

LATER = "5b builds this surface; the session removes this mark when it makes the test pass"


def profit_and_value(facts: dict[str, Any], feedback: str) -> Any:
    """A narrative that quotes two figures, so a change to either shows."""
    from feasibility.llm.narrative_check import NarrativeDraft

    figures_ = facts["figures"]
    return NarrativeDraft(
        summary=f"Profit is {figures_['profit']} on a finished value of {figures_['arv']}.",
        risks=[],
        checks_before_offer=[],
    )


@pytest.fixture(scope="module")
def plain(migrated_engine: Engine) -> Iterator[Any]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = RunModel(draft=profit_and_value, **FREE)
    one = run_day(migrated_engine, _settings(), DAY_ONE, model)
    two = run_day(migrated_engine, _settings(), DAY_TWO, model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


def facts_of(engine: Engine, run_id: int, candidate_id: int) -> Any:
    result = rows(
        engine,
        "SELECT result FROM proforma WHERE run_id = :r AND candidate_id = :c",
        r=run_id,
        c=candidate_id,
    )[0].result
    signals = rows(
        engine,
        "SELECT result FROM candidate_signals WHERE run_id = :r AND candidate_id = :c",
        r=run_id,
        c=candidate_id,
    )
    from feasibility.llm.results import SignalsResult

    return build_facts(
        ProformaResult.model_validate(result),
        SignalsResult.model_validate(signals[0].result) if signals else None,
    )


def digits_outside(text: str, figure_strings: list[str]) -> list[str]:
    """Digit runs left in `text` once every allowed figure string is taken out, longest first."""
    for figure in sorted(set(figure_strings), key=len, reverse=True):
        text = text.replace(figure, " ")
    return re.findall(r"\d+", text)


def decimals_in(node: Any) -> set[Decimal]:
    """Every value in a stored result that is, or reads as, a decimal number."""
    found: set[Decimal] = set()
    if isinstance(node, dict):
        for value in node.values():
            found |= decimals_in(value)
    elif isinstance(node, list):
        for value in node:
            found |= decimals_in(value)
    elif isinstance(node, str | int | float) and not isinstance(node, bool):
        with contextlib.suppress(ArithmeticError):
            found.add(Decimal(str(node)))
    return found


# --- the brief ----------------------------------------------------------------------------------


def test_b1_every_number_in_the_brief_traces_to_a_stored_value(plain: Any) -> None:
    engine, one, two = plain
    for run in (one, two):
        brief = build_with(engine, run.run_id)
        assert brief.candidates
        for entry in brief.candidates:
            result = rows(
                engine,
                "SELECT result FROM proforma WHERE run_id = :r AND candidate_id = :c",
                r=run.run_id,
                c=entry.candidate_id,
            )[0].result
            stored = decimals_in(result)
            for name, value in entry.figures.model_dump().items():
                assert value is None or Decimal(value) in stored, (entry.candidate_id, name)
            comps = entry.comps
            for value in (comps.median_psf, comps.price_low, comps.price_high):
                assert value is None or Decimal(value) in stored
            facts = facts_of(engine, run.run_id, entry.candidate_id)
            narrative = entry.narrative
            if narrative.status == "accepted":
                spoken = [narrative.summary or "", *[r.text for r in narrative.risks]]
                spoken += narrative.checks_before_offer
                assert digits_outside(" ".join(spoken), list(facts.figures.values())) == []
        counts = brief.not_shown
        assert brief.shown == len(brief.candidates)
        assert brief.ranked >= brief.shown + counts.no_arv + counts.unsizable


def figure_mutations(profit: str, arv: str) -> list[str]:
    return [
        "Profit is about $108k.",
        "The margin is about 7.6%.",
        "The margin is seven percent.",
        "A comp sold at 554 Ostravelle Ave.",
        "Seller will finance with a note at 6% and 25% down.",
        f"The loss is - {profit}.",
        f"Half of {profit} is the cushion.",
        "Profit is " + "".join(chr(0xFF10 + int(d)) for d in "107") + " thousand.",
        "Profit is " + "".join(chr(0x660 + int(d)) for d in "107") + " thousand.",
        "The hold is XIV months.",
        f"The profit is {arv}.",
    ]


def test_b2_a_changed_number_in_an_accepted_narrative_is_withheld(plain: Any) -> None:
    engine, one, _ = plain
    base = build_with(engine, one.run_id)
    verdicts = {e.candidate_id: e.verdict[0] for e in base.candidates}
    sample = base.candidates[0]
    profit = figures.money(Decimal(sample.figures.profit))
    arv = figures.money(Decimal(sample.figures.arv))
    # The "profit is <ARV>" relabelling is a known gap (B12), tested apart.
    for said in figure_mutations(profit, arv)[:-1]:
        updates = tamper_narratives(one.run_id, narrative_rows(engine, one.run_id), verdicts, said)

        brief = build_with(engine, one.run_id, *updates)

        assert {e.narrative.status for e in brief.candidates} == {"withheld"}, said
        assert "108k" not in brief.model_dump_json()


@pytest.mark.xfail(
    strict=True,
    reason="a known gap, recorded in ARCHITECTURE: a delivered figure under another figure's name "
    "passes the figure check, which tests that a figure is real and not what it is called",
)
def test_b12_a_figure_under_another_figures_name_is_withheld(plain: Any) -> None:
    engine, one, _ = plain
    base = build_with(engine, one.run_id)
    verdicts = {e.candidate_id: e.verdict[0] for e in base.candidates}
    arv = figures.money(Decimal(base.candidates[0].figures.arv))
    updates = tamper_narratives(
        one.run_id, narrative_rows(engine, one.run_id), verdicts, f"The profit is {arv}."
    )

    brief = build_with(engine, one.run_id, *updates)

    assert {e.narrative.status for e in brief.candidates} == {"withheld"}


def test_b3_a_pro_forma_changed_after_acceptance_withholds_the_narrative(plain: Any) -> None:
    engine, one, _ = plain
    base = build_with(engine, one.run_id)
    quoting = [
        row.candidate_id
        for row in rows(
            engine,
            "SELECT candidate_id, result FROM candidate_narrative WHERE run_id = :r "
            "AND status = 'accepted' AND result->'figures_quoted' @> '[{\"key\": \"profit\"}]'",
            r=one.run_id,
        )
    ]
    assert quoting, "the scripted narrative must quote the profit for this test to mean anything"
    target = quoting[0]
    stored = rows(
        engine,
        "SELECT result, profit FROM proforma WHERE run_id = :r AND candidate_id = :c",
        r=one.run_id,
        c=target,
    )[0]
    bumped = Decimal(stored.profit) + Decimal("0.01")
    result = {**stored.result}
    result["totals"] = {**result["totals"], "profit": str(bumped)}
    updates = [
        (
            "UPDATE proforma SET result = CAST(:j AS jsonb), profit = :p "
            "WHERE run_id = :r AND candidate_id = :c",
            {"j": json.dumps(result), "p": bumped, "r": one.run_id, "c": target},
        )
    ]

    try:
        brief = build_with(engine, one.run_id, *updates)
    except BriefError:
        return
    entry = next(e for e in base.candidates if e.candidate_id == target)
    changed = next(e for e in brief.candidates if e.candidate_id == target)
    assert entry.narrative.status == "accepted"
    assert changed.narrative.status == "withheld"


def test_b4_the_result_and_its_columns_cannot_disagree(plain: Any) -> None:
    engine, one, _ = plain
    base = build_with(engine, one.run_id)
    target = base.candidates[0].candidate_id
    stored = rows(
        engine,
        "SELECT result FROM proforma WHERE run_id = :r AND candidate_id = :c",
        r=one.run_id,
        c=target,
    )[0].result
    result = {**stored, "totals": {**stored["totals"], "profit": "1.00"}}
    update = (
        "UPDATE proforma SET result = CAST(:j AS jsonb) WHERE run_id = :r AND candidate_id = :c",
        {"j": json.dumps(result), "r": one.run_id, "c": target},
    )

    try:
        brief = build_with(engine, one.run_id, update)
    except BriefError:
        return
    column = rows(
        engine,
        "SELECT profit FROM proforma WHERE run_id = :r AND candidate_id = :c",
        r=one.run_id,
        c=target,
    )[0].profit
    shown = next(e for e in brief.candidates if e.candidate_id == target)
    assert Decimal(shown.figures.profit) == column


def test_b10_the_comp_count_must_match_the_comps_used(plain: Any) -> None:
    engine, one, _ = plain
    target = build_with(engine, one.run_id).candidates[0].candidate_id
    update = (
        "UPDATE proforma SET result = jsonb_set(result, '{arv,comp_count_used}', '99') "
        "WHERE run_id = :r AND candidate_id = :c",
        {"r": one.run_id, "c": target},
    )

    try:
        brief = build_with(engine, one.run_id, update)
    except BriefError:
        return
    shown = next(e for e in brief.candidates if e.candidate_id == target)
    assert shown.comps.count_used != 99


def test_b11_a_comps_street_name_in_a_narrative_is_withheld(plain: Any) -> None:
    engine, one, _ = plain
    base = build_with(engine, one.run_id)
    verdicts = {e.candidate_id: e.verdict[0] for e in base.candidates}
    comps = rows(
        engine,
        "SELECT result->'arv'->'comps' AS comps FROM proforma WHERE run_id = :r "
        "AND status = 'computed' LIMIT 1",
        r=one.run_id,
    )[0].comps
    street = comps[0]["address"].split(" ", 1)[1].split(",")[0].title()
    updates = tamper_narratives(
        one.run_id,
        narrative_rows(engine, one.run_id),
        verdicts,
        f"A comparable sale on {street} supports the value.",
    )

    brief = build_with(engine, one.run_id, *updates)

    assert {e.narrative.status for e in brief.candidates} == {"withheld"}


def test_b9_the_counts_add_up(plain: Any) -> None:
    engine, one, _ = plain
    update = (
        "DELETE FROM proforma WHERE run_id = :r AND candidate_id = "
        "(SELECT candidate_id FROM proforma WHERE run_id = :r AND status = 'no_arv' LIMIT 1)",
        {"r": one.run_id},
    )

    brief = build_with(engine, one.run_id, update)

    not_shown = brief.not_shown.model_dump()
    assert brief.ranked == brief.shown + sum(not_shown.values())
    assert not_shown["no_pro_forma"] == 1


# --- when the brief is built --------------------------------------------------------------------


def test_b7_a_brief_is_built_from_one_snapshot_of_the_database(plain: Any) -> None:
    from sqlalchemy import event

    from feasibility.delivery.build import build_and_store

    engine, one, _ = plain
    fired: list[bool] = []

    def tear(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        is_read = statement.lstrip().upper().startswith("SELECT")
        if "candidate_narrative" in statement and is_read and not fired:
            fired.append(True)
            with engine.begin() as other:
                other.execute(
                    text(
                        "UPDATE candidate_narrative SET result = jsonb_set(result, "
                        "'{summary}', '\"TORN\"') WHERE run_id = :r"
                    ),
                    {"r": one.run_id},
                )

    event.listen(engine, "before_cursor_execute", tear)
    try:
        brief, _ = build_and_store(engine, one.run_id)
    finally:
        event.remove(engine, "before_cursor_execute", tear)
        execute(
            engine,
            "UPDATE candidate_narrative SET result = jsonb_set(result, '{summary}', "
            "'\"A plain summary of where this candidate stands.\"') WHERE run_id = :r",
            r=one.run_id,
        )

    assert fired
    assert "TORN" not in brief.model_dump_json()


def test_b8_the_briefs_data_mode_is_the_runs_not_the_readers(plain: Any) -> None:
    from feasibility.config import DataMode, Settings
    from feasibility.jobs.handlers import JobContext, run_brief_deliver
    from feasibility.jobs.payloads import BriefDeliverPayload

    engine, one, _ = plain
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key="x" * 20
    )

    run_brief_deliver(BriefDeliverPayload(run_id=one.run_id), JobContext(engine, live, 1))

    assert (
        rows(
            engine, "SELECT content->>'data_mode' AS m FROM brief WHERE run_id = :r", r=one.run_id
        )[0].m
        == "mock"
    )


# --- the surfaces 5b builds ---------------------------------------------------------------------


def test_b13_control_the_digit_scan_finds_an_invented_number() -> None:
    from attack_support import allowed_displays, digits_left_over

    allowed = allowed_displays({"profit": "107560.14", "margin": "0.0757", "rank": 3})

    assert digits_left_over("Profit $107,560.14, margin 7.57%, rank 3.", allowed) == []
    assert digits_left_over("Profit $108,000.00 and rank 3.", allowed) == ["108", "000", "00"]
    assert digits_left_over("Margin 7.6%.", allowed) == ["7", "6"]
    assert digits_left_over("Profit $7,560.14.", allowed) != []  # not a piece of a longer figure


def test_b13_control_a_model_written_number_does_not_excuse_itself() -> None:
    from attack_support import allowed_displays, digits_left_over, without_model_text

    dump = {
        "candidates": [
            {
                "profit": "107560.14",
                "narrative": {
                    "summary": "Profit is about 108,000.",
                    "risks": [{"text": "Rent 2,345 a month."}],
                    "checks_before_offer": ["Verify 77 things."],
                },
            }
        ]
    }

    allowed = allowed_displays(without_model_text(dump))

    assert digits_left_over("Profit is about 108,000.", allowed) != []
    assert digits_left_over("Rent 2,345 a month. Verify 77 things.", allowed) != []
    assert digits_left_over("Profit is $107,560.14.", allowed) == []


def test_b13_every_figure_in_the_pdf_is_a_formatted_stored_value(plain: Any) -> None:
    import io

    from attack_support import allowed_displays, digits_left_over, without_model_text
    from pypdf import PdfReader

    from feasibility.delivery.document import proforma_document
    from feasibility.delivery.pdf import render_pdf

    engine, one, _ = plain
    brief = build_with(engine, one.run_id)
    entry = brief.candidates[0]
    stored = rows(
        engine,
        "SELECT result FROM proforma WHERE run_id = :r AND candidate_id = :c",
        r=one.run_id,
        c=entry.candidate_id,
    )[0].result
    document = proforma_document(brief, entry, ProformaResult.model_validate(stored))

    reader = PdfReader(io.BytesIO(render_pdf(document)))
    text_ = "\n".join(page.extract_text() for page in reader.pages)
    # Stored values, the page numbers ("Page n of m", at most three pages) and nothing else.
    allowed = allowed_displays(without_model_text(brief.model_dump(mode="json")), stored, [1, 2, 3])

    assert digits_left_over(text_, allowed) == []
    assert "7.6%" not in text_


def test_b14_every_visible_number_in_the_slack_payload_is_a_stored_value(plain: Any) -> None:
    from attack_support import allowed_displays, digits_left_over, without_model_text

    from feasibility.delivery.slack import digest_blocks

    engine, one, _ = plain
    brief = build_with(engine, one.run_id)

    blocks, fallback = digest_blocks(brief)

    visible = " ".join([fallback, *[leaf for leaf in leaves_of(blocks) if isinstance(leaf, str)]])
    allowed = allowed_displays(without_model_text(brief.model_dump(mode="json")))
    assert digits_left_over(visible, allowed) == []


def leaves_of(node: Any) -> list[Any]:
    from attack_support import leaves

    return leaves(node)


def test_b15_the_notion_numbers_equal_the_stored_decimals(plain: Any) -> None:
    from feasibility.delivery.notion import row_properties

    engine, one, _ = plain
    brief = build_with(engine, one.run_id)
    entry = brief.candidates[0]
    properties = row_properties(brief, entry)
    assert Decimal(str(properties["ARV"]["number"])) == Decimal(entry.figures.arv)
    assert Decimal(str(properties["Margin"]["number"])) == Decimal(entry.figures.margin)


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE proforma SET result = jsonb_set(result, '{totals,roi}', '\"9.9999\"') "
        "WHERE run_id = :r",
        "UPDATE proforma SET result = jsonb_set(result, '{totals,annualized_return}', "
        "'\"9.9999\"') WHERE run_id = :r",
        "UPDATE proforma SET flags = flags || '[\"extra_flag\"]'::jsonb WHERE run_id = :r",
        "UPDATE proforma SET estimate_fetched_on = estimate_fetched_on - 1 WHERE run_id = :r",
    ],
)
def test_b4_the_other_columns_must_agree_with_the_result_too(plain: Any, tamper: str) -> None:
    engine, one, _ = plain

    with pytest.raises(BriefError, match="disagrees"):
        build_with(engine, one.run_id, (tamper, {"r": one.run_id}))


# These two empty the database for themselves, so they run after every test that shares `plain`.
def test_b6_a_brief_cannot_be_built_while_the_run_still_has_stages_to_go(
    migrated_engine: Engine,
) -> None:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    outcomes: list[str] = []

    def hook(prompt_id: str) -> None:
        if prompt_id != "narrative.write" or outcomes:
            return
        run_id = rows(migrated_engine, "SELECT id FROM sourcing_run")[0].id
        try:
            build_with(migrated_engine, run_id)
            outcomes.append("built")
        except BriefNotReadyError:
            outcomes.append("not ready")

    try:
        run_day(migrated_engine, _settings(), DAY_ONE, RunModel(before_call=hook, **FREE))

        assert outcomes == ["not ready"]
    finally:
        empty_database(migrated_engine)


def test_b5_the_endpoint_never_serves_a_stale_brief(migrated_engine: Engine) -> None:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    try:
        first = run_day(migrated_engine, _settings(), DAY_ONE, RunModel(**FREE))
        with migrated_engine.begin() as connection:
            store.write_brief(connection, build_brief(connection, first.run_id))
        # The same day again with an empty cache and a different narrative: the rows the brief
        # was built from change.
        execute(migrated_engine, "DELETE FROM llm_result")
        run_day(
            migrated_engine,
            _settings(),
            DAY_ONE,
            RunModel(
                draft=lambda facts, feedback: __import__(
                    "feasibility.llm.narrative_check", fromlist=["NarrativeDraft"]
                ).NarrativeDraft(
                    summary="A different plain summary.", risks=[], checks_before_offer=[]
                ),
                **FREE,
            ),
        )
        with TestClient(create_app(_settings(), migrated_engine)) as client:
            response = client.get(f"/sourcing/runs/{first.run_id}/brief", headers=READ_HEADERS)

        if response.status_code == 200:
            fresh = build_with(migrated_engine, first.run_id)
            assert Brief.model_validate(response.json()["brief"]) == fresh
        else:
            assert response.status_code in (404, 409)
    finally:
        empty_database(migrated_engine)

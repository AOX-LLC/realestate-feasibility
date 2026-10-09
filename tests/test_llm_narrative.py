"""The narrative flow: prompt, one repair attempt, and the stored result."""

import asyncio
import re
from decimal import Decimal
from typing import Any, get_args

import pytest
from aox_agent_core import AgentCoreConfig, Mode
from aox_agent_core.errors import ModelRefusalError
from llm_fakes import Narrator
from proforma_cases import ASSUMPTIONS, TTL_DAYS, s1, s3
from pydantic import ValidationError
from sqlalchemy import Engine, select

from feasibility.config import Settings
from feasibility.llm import narrative
from feasibility.llm.client import build_model_client
from feasibility.llm.facts import FIGURE_KEYS, build_facts
from feasibility.llm.metered import MeteredClient, input_sha256
from feasibility.llm.narrative import (
    NARRATIVE_PROMPT,
    NarrativeResult,
    narrative_inputs,
    repair_feedback,
    write_narrative,
    write_narrative_sync,
)
from feasibility.llm.narrative_check import NarrativeDraft, RiskPoint, Violation
from feasibility.llm.spend import SessionSpendGuard
from feasibility.proforma.engine import build_proforma
from feasibility.tables import llm_call

PERSONAL_DATA_NAME = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal")
FACTS = build_facts(build_proforma(s1(), ASSUMPTIONS, estimate_ttl_days=TTL_DAYS), None)

GOOD = NarrativeDraft(
    summary="Profit is $107,560.14 at a 7.57% margin, short of the target.",
    risks=[RiskPoint(basis=["below_target"], text="The margin is below the 15.00% target.")],
    checks_before_offer=["Confirm the $324,271.50 ceiling with a bid."],
)
BAD = NarrativeDraft(
    summary="Profit is about 108k and two lots help.",
    risks=[RiskPoint(basis=["made_up"], text="A risk.")],
    checks_before_offer=[],
)
ALSO_BAD = NarrativeDraft(summary="Profit is $107,560.", risks=[], checks_before_offer=[])


@pytest.fixture(scope="module")
def config() -> AgentCoreConfig:
    return build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]


def metered(narrator: Narrator, engine: Engine | None, config: AgentCoreConfig) -> MeteredClient:
    return MeteredClient(
        narrator,
        SessionSpendGuard(Decimal(100)),
        engine,
        config.model_copy(update={"mode": Mode.REPLAY}),
    )


# --- the flow ---------------------------------------------------------------------------------


def test_a_clean_first_draft_is_accepted_with_one_call_and_one_ledger_id(
    engine: Engine, config: AgentCoreConfig
) -> None:
    narrator = Narrator(GOOD)

    attempt = write_narrative_sync(metered(narrator, engine, config), FACTS, stage="narrative")

    assert attempt.draft == GOOD
    assert attempt.check.passed is True
    assert attempt.attempts == 1
    assert len(attempt.llm_call_ids) == 1
    assert attempt.cost == Decimal("0.012000")
    assert narrator.inputs == [{"facts": FACTS.as_inputs(), "feedback": ""}]
    with engine.connect() as connection:
        (row,) = connection.execute(select(llm_call.c.id, llm_call.c.stage)).all()
    assert (row.id, row.stage) == (attempt.llm_call_ids[0], "narrative")


def test_a_failed_first_draft_gets_one_repair_with_the_violations_as_feedback(
    engine: Engine, config: AgentCoreConfig
) -> None:
    narrator = Narrator(BAD, GOOD)

    attempt = write_narrative_sync(metered(narrator, engine, config), FACTS, stage="narrative")

    assert attempt.draft == GOOD
    assert attempt.attempts == 2
    assert len(attempt.llm_call_ids) == 2
    assert len(set(attempt.llm_call_ids)) == 2
    assert attempt.cost == Decimal("0.024000")
    first, second = narrator.inputs
    assert first["feedback"] == ""
    assert second["facts"] == first["facts"]
    feedback = str(second["feedback"])
    assert "unlisted_figure: 108k" in feedback
    assert "spelled_number: two" in feedback
    assert "unknown_basis: made_up" in feedback
    assert "Profit is about" not in feedback  # the draft's own sentences are not sent back
    with engine.connect() as connection:
        assert len(connection.execute(select(llm_call.c.id)).all()) == 2


def test_two_failures_reject_and_keep_no_text(engine: Engine, config: AgentCoreConfig) -> None:
    narrator = Narrator(BAD, ALSO_BAD)

    attempt = write_narrative_sync(metered(narrator, engine, config), FACTS, stage="narrative")

    assert attempt.draft is None
    assert attempt.attempts == 2
    assert attempt.check.passed is False
    assert [(v.kind, v.text) for v in attempt.check.violations] == [("unlisted_figure", "$107,560")]
    assert len(narrator.inputs) == 2  # never a third call


def test_a_provider_error_on_the_repair_propagates_and_both_calls_are_in_the_ledger(
    engine: Engine, config: AgentCoreConfig
) -> None:
    narrator = Narrator(BAD, ModelRefusalError("no"))

    with pytest.raises(ModelRefusalError):
        write_narrative_sync(metered(narrator, engine, config), FACTS, stage="narrative")

    with engine.connect() as connection:
        outcomes = connection.execute(select(llm_call.c.outcome).order_by(llm_call.c.id)).scalars()
        assert list(outcomes) == ["ok", "refusal"]


def test_the_async_flow_sends_the_same_inputs_as_the_sync_one(config: AgentCoreConfig) -> None:
    sync_narrator, async_narrator = Narrator(BAD, GOOD), Narrator(BAD, GOOD)

    sync_attempt = write_narrative_sync(metered(sync_narrator, None, config), FACTS, stage="eval")
    async_attempt = asyncio.run(
        write_narrative(metered(async_narrator, None, config), FACTS, "eval")
    )

    assert sync_narrator.inputs == async_narrator.inputs
    assert sync_attempt == async_attempt
    assert sync_attempt.llm_call_ids == []  # no database, no ledger rows


def test_the_attempt_names_the_tier_and_hash_of_the_first_call(config: AgentCoreConfig) -> None:
    attempt = write_narrative_sync(metered(Narrator(BAD, GOOD), None, config), FACTS, stage="eval")

    first_inputs = narrative_inputs(FACTS)
    from aox_agent_core import Tier

    assert attempt.tier == "mid"
    assert attempt.model == "a-mid-model"
    assert attempt.input_sha256 == input_sha256(NARRATIVE_PROMPT, Tier.MID, first_inputs)


# --- the prompt and the inputs ----------------------------------------------------------------


def test_the_prompt_is_the_documented_one_and_renders_with_no_unfilled_placeholder() -> None:
    rendered = NARRATIVE_PROMPT.render(narrative_inputs(FACTS))

    assert (NARRATIVE_PROMPT.id, NARRATIVE_PROMPT.version) == ("narrative.write", 1)
    assert "${" not in rendered
    assert rendered.startswith("<facts>\n{")
    assert "<feedback>\n\n</feedback>" in rendered


def test_the_system_prompt_explains_every_figure_key() -> None:
    system = NARRATIVE_PROMPT.system or ""

    for key in FIGURE_KEYS:
        assert f"- {key}:" in system


def test_the_system_prompt_states_the_figure_rule_and_where_signals_come_from() -> None:
    system = (NARRATIVE_PROMPT.system or "").lower()

    assert "copied exactly" in system
    assert "never write a digit" in system
    assert "a second lot" in system
    assert "never put a dash" in system
    assert "come from code" in system
    assert "never compare" in system


def test_the_system_prompt_has_no_digit_for_the_model_to_echo() -> None:
    assert not re.search(r"[0-9]", NARRATIVE_PROMPT.system or "")


def test_the_facts_input_has_no_address_and_no_remarks_text() -> None:
    rendered = NARRATIVE_PROMPT.render(narrative_inputs(FACTS))

    assert "Comp St" not in rendered and "Dallas" not in rendered


def test_feedback_lists_each_violation_as_kind_and_token_and_nothing_else() -> None:
    text = repair_feedback(
        [
            Violation(kind="unlisted_figure", text="$107,560"),
            Violation(kind="too_long", text="summary"),
        ]
    )

    assert "unlisted_figure: $107,560" in text
    assert "too_long: summary" in text
    assert text.count("\n") == text.strip().count("\n")  # no trailing blank lines


def test_feedback_keeps_a_long_token_short() -> None:
    text = repair_feedback([Violation(kind="unlisted_figure", text="9" * 500)])

    assert len(text) < 400


def test_no_field_the_model_fills_is_a_number() -> None:
    def flatten(annotation: Any) -> list[Any]:
        return [annotation, *(a for arg in get_args(annotation) for a in flatten(arg))]

    for model in (NarrativeDraft, RiskPoint):
        for field in model.model_fields.values():
            assert {int, float, Decimal}.isdisjoint(flatten(field.annotation)), (model, field)


# --- the stored result ------------------------------------------------------------------------


def accepted_result() -> NarrativeResult:
    attempt = write_narrative_sync(
        metered(Narrator(GOOD), None, build_model_client(Settings(_env_file=None)).config),  # type: ignore[call-arg]
        FACTS,
        stage="eval",
    )
    return narrative.accepted_result(attempt, FACTS)


def test_an_accepted_result_keeps_the_text_the_figures_and_the_facts() -> None:
    result = accepted_result()
    document = result.model_dump(mode="json")

    assert (result.status, result.reason) == ("accepted", None)
    assert result.summary == GOOD.summary
    assert [(f.key, f.text) for f in result.figures_quoted] == [
        ("margin", "7.57%"),
        ("max_offer", "$324,271.50"),
        ("profit", "$107,560.14"),
        ("target_margin", "15.00%"),
    ]
    assert document["facts"] == {"figures": FACTS.figures, "codes": FACTS.codes}
    assert document["check"] == {"passed": True, "attempts": 1, "violations": []}
    assert document["model"]["reused"] is False
    assert document["model"]["tier"] == "mid"


def test_a_stored_result_round_trips() -> None:
    result = accepted_result()

    assert NarrativeResult.model_validate(result.model_dump(mode="json")) == result


def test_a_rejected_result_has_the_violations_and_no_text(config: AgentCoreConfig) -> None:
    attempt = write_narrative_sync(
        metered(Narrator(BAD, ALSO_BAD), None, config), FACTS, stage="eval"
    )

    result = narrative.rejected_result(attempt, FACTS)
    dumped = result.model_dump_json()

    assert (result.status, result.reason) == ("rejected", "figure_check")
    assert result.summary is None and result.risks == [] and result.figures_quoted == []
    assert "Profit is" not in dumped
    assert result.check is not None and result.check.attempts == 2
    assert [v.kind for v in result.check.violations] == ["unlisted_figure"]


def test_a_basis_only_rejection_is_a_basis_check(config: AgentCoreConfig) -> None:
    only_basis = NarrativeDraft(
        summary="Fine.",
        risks=[RiskPoint(basis=["made_up"], text="A risk.")],
        checks_before_offer=[],
    )
    attempt = write_narrative_sync(
        metered(Narrator(only_basis, only_basis), None, config), FACTS, stage="eval"
    )

    assert narrative.rejected_result(attempt, FACTS).reason == "basis_check"


def test_the_result_models_enforce_what_each_status_holds() -> None:
    facts = narrative.StoredFacts(figures={}, codes=[])
    with pytest.raises(ValidationError, match="reason"):
        NarrativeResult(status="accepted", reason="budget", facts=facts)
    with pytest.raises(ValidationError, match="reason"):
        NarrativeResult(status="deferred", reason=None, facts=facts)
    with pytest.raises(ValidationError, match="text"):
        NarrativeResult(status="deferred", reason="budget", summary="Text.", facts=facts)
    with pytest.raises(ValidationError, match="summary"):
        NarrativeResult(status="accepted", reason=None, facts=facts)
    with pytest.raises(ValidationError, match="check"):
        NarrativeResult(
            status="rejected",
            reason="figure_check",
            facts=facts,
            check=narrative.StoredCheck(passed=False, attempts=2, violations=[]),
        )


def test_non_attempted_statuses_have_no_check_and_empty_facts_are_allowed() -> None:
    result = NarrativeResult(
        status="not_eligible",
        reason="proforma_no_arv",
        facts=narrative.StoredFacts(figures={}, codes=[]),
    )

    assert result.check is None and result.model is None
    assert NarrativeResult.model_validate(result.model_dump(mode="json")) == result


def test_unknown_keys_and_other_versions_are_refused() -> None:
    document = accepted_result().model_dump(mode="json")

    with pytest.raises(ValidationError):
        NarrativeResult.model_validate({**document, "surprise": 1})
    with pytest.raises(ValidationError):
        NarrativeResult.model_validate({**document, "version": 2})


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(k for item in value.values() for k in _keys(item))]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


def test_no_stored_key_matches_the_personal_data_name_pattern() -> None:
    keys = _keys(accepted_result().model_dump(mode="json"))

    assert [key for key in keys if PERSONAL_DATA_NAME.search(key)] == []


def test_s3_facts_build_a_valid_narrative_flow_input() -> None:
    facts = build_facts(build_proforma(s3(), ASSUMPTIONS, estimate_ttl_days=TTL_DAYS), None)

    rendered = NARRATIVE_PROMPT.render(narrative_inputs(facts))

    assert "no_viable_offer" in rendered and "max_offer" not in facts.figures

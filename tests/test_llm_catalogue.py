"""The signal catalogue is the single definition of the twelve remarks signals."""

import re
from typing import get_args

from feasibility.llm.catalogue import (
    CATALOGUE,
    CODES,
    DEFINITIONS,
    LITERAL_CODES,
    SignalCode,
)

# The same pattern the schema and API tests apply to column names and JSON keys.
PERSONAL_DATA_NAME = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal")


def test_the_literal_and_the_table_list_the_same_twelve_codes() -> None:
    assert len(CODES) == 12
    assert len(set(CODES)) == 12
    assert set(CODES) == set(get_args(SignalCode)) == set(LITERAL_CODES)


def test_every_signal_has_a_polarity_a_meaning_and_edge_notes() -> None:
    for definition in CATALOGUE:
        assert definition.polarity in {"risk", "opportunity"}
        assert definition.meaning.strip()
        assert definition.include.strip()
        assert definition.exclude.strip()
        assert DEFINITIONS[definition.code] is definition


def test_the_plans_polarities_hold() -> None:
    opportunities = {d.code for d in CATALOGUE if d.polarity == "opportunity"}

    assert opportunities == {
        "teardown_language",
        "plans_or_permits",
        "seller_financing",
        "multiple_lots",
    }


def test_no_code_matches_the_personal_data_name_pattern() -> None:
    assert [code for code in CODES if PERSONAL_DATA_NAME.search(code)] == []

"""check_narrative: the heart of Phase 4's rule zero, tested far beyond the eval.

Every digit in a narrative must sit inside a figure string the facts sheet gave the model."""

import pytest
from proforma_cases import ASSUMPTIONS, TTL_DAYS, s1

from feasibility.llm.facts import Facts, build_facts
from feasibility.llm.narrative_check import (
    NarrativeDraft,
    RiskPoint,
    check_narrative,
)
from feasibility.proforma.engine import build_proforma

FACTS = build_facts(build_proforma(s1(), ASSUMPTIONS, estimate_ttl_days=TTL_DAYS), None)
BASIS = ["below_target"]


def draft(
    summary: str,
    risks: list[tuple[list[str], str]] | None = None,
    checks: list[str] | None = None,
) -> NarrativeDraft:
    return NarrativeDraft(
        summary=summary,
        risks=[RiskPoint(basis=basis, text=text) for basis, text in risks or []],
        checks_before_offer=checks or [],
    )


def kinds(result_draft: NarrativeDraft, facts: Facts = FACTS) -> list[tuple[str, str]]:
    return [(v.kind, v.text) for v in check_narrative(result_draft, facts).violations]


# --- accepted ----------------------------------------------------------------------------------

ACCEPTED = [
    ("exact_figures", "Profit is $107,560.14 at a 7.57% margin.", {"profit", "margin"}),
    ("figure_ends_the_sentence", "The ceiling is $324,271.50.", {"max_offer"}),
    ("negative_money", "The offer is -$95,728.50 from the ceiling.", {"headroom_vs_offer"}),
    ("in_parentheses", "The value ($1,420,799.85) rests on comps.", {"arv"}),
    ("before_a_comma", "At $420,000.00, the offer is high.", {"offer_price"}),
    ("area_and_months", "A 3,168 sq ft home held for 9 months.", {"buildable_sqft", "hold_months"}),
    ("count", "Priced from 7 comps on a 6,400 sq ft lot.", {"comp_count_used", "lot_sqft"}),
    ("stress_cases", "It still makes $84,670.08 with a longer hold.", {"profit_if_hold_longer"}),
    ("prose_words", "One risk stands out; a single lot, half the room, double or twice.", set()),
    ("ordinal_prose", "A second lot would help; the first step is a survey.", set()),
    ("sentence_after_a_figure", "Profit is $107,560.14. Maybe more later.", {"profit"}),
    ("word_after_a_figure", "Profit is $107,560.14 but the margin is thin.", {"profit"}),
    ("no_numbers", "The margin falls short of the target.", set()),
    ("percent_symbol_stands_alone", "A margin of 15.00% is the target.", {"target_margin"}),
]


@pytest.mark.parametrize(("name", "text", "keys"), ACCEPTED, ids=[row[0] for row in ACCEPTED])
def test_accepted(name: str, text: str, keys: set[str]) -> None:
    result = check_narrative(draft(text), FACTS)

    assert result.violations == [], name
    assert result.passed is True
    assert {figure.key for figure in result.figures_quoted} == keys


def test_every_figure_of_the_sheet_is_accepted_in_one_text() -> None:
    text = "; ".join(FACTS.figures.values())

    result = check_narrative(draft(text), FACTS)

    assert result.violations == []
    assert {f.key for f in result.figures_quoted} == set(FACTS.figures)


def test_a_figure_used_twice_is_reported_once() -> None:
    result = check_narrative(draft("Profit $107,560.14, again $107,560.14."), FACTS)

    assert [(f.key, f.text) for f in result.figures_quoted] == [("profit", "$107,560.14")]


def test_figures_in_risks_and_checks_are_found_too() -> None:
    result = check_narrative(
        draft(
            "Short summary.",
            risks=[(BASIS, "The margin is 7.57%.")],
            checks=["Confirm the $420,000.00 price."],
        ),
        FACTS,
    )

    assert result.violations == []
    assert {f.key for f in result.figures_quoted} == {"margin", "offer_price"}


def test_two_keys_with_the_same_text_are_both_reported() -> None:
    facts = FACTS.model_copy(update={"figures": {"profit": "$5.00", "total_cost": "$5.00"}})

    result = check_narrative(draft("It is $5.00."), facts)

    assert [f.key for f in result.figures_quoted] == ["profit", "total_cost"]


# --- rejected: a number that is not a figure string --------------------------------------------

REJECTED = [
    ("rounded_money", "Profit is about $107,560.", [("unlisted_figure", "$107,560")]),
    ("rounded_percent", "A 7.6% margin.", [("unlisted_figure", "7.6%")]),
    ("k_suffix", "Profit near 108k.", [("unlisted_figure", "108k")]),
    ("millions_suffix", "An ARV of $1.4M.", [("unlisted_figure", "$1.4M")]),
    ("a_year", "Priced in 2026.", [("unlisted_figure", "2026")]),
    ("lot_dimensions", "A 50x150 lot.", [("unlisted_figure", "50x150")]),
    ("changed_digit", "Profit is $107,560.15.", [("unlisted_figure", "$107,560.15")]),
    ("truncated", "Profit is $107,560.1.", [("unlisted_figure", "$107,560.1")]),
    ("dropped_cents", "ARV $1,420,799.", [("unlisted_figure", "$1,420,799")]),
    ("extra_leading_digit", "ARV $11,420,799.85.", [("unlisted_figure", "$11,420,799.85")]),
    (
        "prefix_inside_longer",
        "ARV $1,420,799.85 or $420,799.85.",
        [("unlisted_figure", "$420,799.85")],
    ),
    ("spaced_k", "Profit is $107,560.14 k.", [("unlisted_figure", "$107,560.14")]),
    ("spaced_m", "An ARV of $1,420,799.85 M.", [("unlisted_figure", "$1,420,799.85")]),
    ("spaced_mm", "A cost of $1,313,239.71 mm.", [("unlisted_figure", "$1,313,239.71")]),
    ("money_as_percent", "A $420,000.00% offer.", [("unlisted_figure", "$420,000.00%")]),
    ("area_as_money", "A $3,168 sq ft home.", [("unlisted_figure", "$3,168")]),
    ("percent_inside_longer", "A 17.57% margin.", [("unlisted_figure", "17.57%")]),
    ("suffixed_figure", "Profit is $107,560.14k.", [("unlisted_figure", "$107,560.14k")]),
    ("prefixed_figure", "Profit is x$107,560.14.", [("unlisted_figure", "x$107,560.14")]),
    ("sign_flipped_percent", "A -7.57% margin.", [("unlisted_figure", "-7.57%")]),
    ("sign_added_money", "A +$324,271.50 ceiling.", [("unlisted_figure", "+$324,271.50")]),
    ("decimal_continues", "A 7.57.5% margin.", [("unlisted_figure", "7.57.5%")]),
    ("space_before_percent", "A 7.57 % margin.", [("unlisted_figure", "7.57")]),
    ("magnitude_after_months", "Held 9 monthsly.", [("unlisted_figure", "9")]),
    (
        "digits_in_a_range",
        "Between $100,000.00 and $200,000.00.",
        [
            ("unlisted_figure", "$100,000.00"),
            ("unlisted_figure", "$200,000.00"),
        ],
    ),
]


@pytest.mark.parametrize(("name", "text", "expected"), REJECTED, ids=[row[0] for row in REJECTED])
def test_rejected_numbers(name: str, text: str, expected: list[tuple[str, str]]) -> None:
    result = check_narrative(draft(text), FACTS)

    assert result.passed is False, name
    assert kinds(draft(text)) == expected


# --- rejected: spelled quantities ----------------------------------------------------------------

SPELLED = [
    ("two", "two lots on the site", ["two"]),
    ("twenty_percent", "twenty percent below", ["percent", "twenty"]),
    ("hundred_and_thousand", "one hundred thousand", ["hundred", "thousand"]),
    ("million", "a million", ["million"]),
    ("billion", "a billion", ["billion"]),
    ("dozen", "a dozen", ["dozen"]),
    ("per_cent", "five per cent more", ["five", "per cent"]),
    ("hyphenated", "thirty-two homes", ["thirty", "two"]),
    ("zero", "zero margin", ["zero"]),
    ("plural_suffix", "dozens and thousands", ["dozens", "thousands"]),
    ("fold", "a tenfold gap", ["tenfold"]),
    ("upper_case", "TWO lots", ["two"]),
    ("ninety_nine", "ninety-nine problems", ["nine", "ninety"]),
]


@pytest.mark.parametrize(("name", "text", "words"), SPELLED, ids=[row[0] for row in SPELLED])
def test_spelled_numbers_are_rejected(name: str, text: str, words: list[str]) -> None:
    result = kinds(draft(text))

    assert result == [("spelled_number", word) for word in words], name


def test_ordinary_words_that_contain_a_number_word_pass() -> None:
    text = "The tenant mentioned a tender offer; the sixty-four-bit joke is gone."

    # "tenant" and "tender" hold "ten" and the last holds "sixty" and "four": only the last fails.
    assert kinds(draft("The tenant has a tender offer and a stone wall.")) == []
    assert [k for k, _ in kinds(draft(text))] == ["spelled_number"] * 2


# --- rejected: characters that could hide a number -----------------------------------------------


# Built from code points: the formatter turns \u escapes into literal characters, which would
# leave invisible ones in the source and trip the ambiguous-character lint.
def _c(*points: int) -> str:
    return "".join(chr(point) for point in points)


ZERO_WIDTH_SPACE = _c(0x200B)
ODD = [
    ("zero_width_inside_two", f"tw{ZERO_WIDTH_SPACE}o lots"),
    ("zero_width_in_figure", f"Profit is $107,560{ZERO_WIDTH_SPACE}.14."),
    ("zero_width_joiner", f"tw{_c(0x200D)}o lots"),
    ("word_joiner", f"tw{_c(0x2060)}o lots"),
    ("byte_order_mark", f"tw{_c(0xFEFF)}o lots"),
    ("cyrillic_o", f"tw{_c(0x043E)} lots"),
    ("greek_omicron", f"tw{_c(0x03BF)} lots"),
    ("arabic_indic_digit", f"Held {_c(0x0665)} months."),
    ("fullwidth_digit", f"A {_c(0xFF17)}.57% margin."),
    ("superscript", f"A 7{_c(0x00B2)} lot."),
    ("vulgar_fraction", f"A {_c(0x00BD)} share."),
    ("circled_digit", f"Item {_c(0x2460)}."),
    ("cjk_numeral", f"{_c(0x4E94)} lots."),
    ("roman_numeral", f"Phase {_c(0x2163)}."),
    ("right_to_left_override", f"Profit {_c(0x202E)}$107,560.14."),
    ("unicode_minus", f"A {_c(0x2212)}$95,728.50 gap."),
    ("fullwidth_percent", f"A 7.57{_c(0xFF05)} margin."),
    ("soft_hyphen", f"tw{_c(0x00AD)}o lots"),
    ("null_byte", "two\x00 lots"),
    ("non_breaking_space", f"A{_c(0x00A0)}margin."),
]


@pytest.mark.parametrize(("name", "text"), ODD, ids=[row[0] for row in ODD])
def test_odd_characters_are_rejected(name: str, text: str) -> None:
    found = [kind for kind, _ in kinds(draft(text))]

    assert "odd_character" in found, name


def test_typographic_marks_are_allowed() -> None:
    quote, dash, ellipsis = _c(0x2019), _c(0x2014), _c(0x2026)
    text = f"Don{quote}t rely on it {dash} the {_c(0x201C)}margin{_c(0x201D)} is thin{ellipsis}"

    assert kinds(draft(text)) == []


def test_a_newline_and_a_tab_are_allowed() -> None:
    assert kinds(draft("First line.\nSecond line.\tDone.")) == []


# --- rejected: basis, size and emptiness ---------------------------------------------------------


def test_an_unknown_basis_code_is_rejected() -> None:
    result = kinds(draft("Fine.", risks=[(["below_target", "made_up_code"], "A risk.")]))

    assert result == [("unknown_basis", "made_up_code")]


def test_a_basis_code_from_the_signals_is_known() -> None:
    facts = FACTS.model_copy(update={"signals": [*FACTS.signals, _signal("as_is_sale")]})

    assert kinds(draft("Fine.", risks=[(["as_is_sale"], "As-is.")]), facts) == []


def _signal(code: str):  # type: ignore[no-untyped-def]
    from feasibility.llm.facts import SignalFact

    return SignalFact(code=code, polarity="risk", quote="Sold as-is, seller makes no repairs")


def test_a_risk_with_no_basis_is_rejected() -> None:
    assert kinds(draft("Fine.", risks=[([], "A risk.")])) == [("unknown_basis", "risks[0].basis")]


def test_more_than_three_basis_codes_is_too_long() -> None:
    codes = ["below_target", "below_target", "below_target", "below_target"]

    assert kinds(draft("Fine.", risks=[(codes, "A risk.")])) == [("too_long", "risks[0].basis")]


def test_an_empty_or_blank_summary_is_rejected() -> None:
    assert kinds(draft("")) == [("empty", "summary")]
    assert kinds(draft("  \n\t ")) == [("empty", "summary")]


def test_blank_risk_and_check_text_is_rejected() -> None:
    result = kinds(draft("Fine.", risks=[(BASIS, " ")], checks=[""]))

    assert result == [("empty", "risks[0].text"), ("empty", "checks_before_offer[0]")]


@pytest.mark.parametrize(
    ("make", "location"),
    [
        (lambda n: draft("a" * n), "summary"),
        (lambda n: draft("Fine.", risks=[(BASIS, "a" * (n - 400))]), "risks[0].text"),
        (lambda n: draft("Fine.", checks=["a" * (n - 500)]), "checks_before_offer[0]"),
    ],
    ids=["summary", "risk_text", "check_text"],
)
def test_length_limits_are_exact(make, location: str) -> None:  # type: ignore[no-untyped-def]
    limits = {"summary": 700, "risks[0].text": 300, "checks_before_offer[0]": 200}
    at_limit = (
        limits[location]
        + {"summary": 0, "risks[0].text": 400, "checks_before_offer[0]": 500}[location]
    )

    assert kinds(make(at_limit)) == []
    assert kinds(make(at_limit + 1)) == [("too_long", location)]


def test_too_many_risks_or_checks_is_too_long() -> None:
    six = [(BASIS, "A risk.")] * 6
    five = [(BASIS, "A risk.")] * 5

    assert kinds(draft("Fine.", risks=five)) == []
    assert kinds(draft("Fine.", risks=six)) == [("too_long", "risks")]
    assert kinds(draft("Fine.", checks=["Check."] * 4)) == []
    assert kinds(draft("Fine.", checks=["Check."] * 5)) == [("too_long", "checks_before_offer")]


def test_violations_come_in_a_stable_order_across_fields() -> None:
    result = kinds(
        draft(
            "About $107,560 and two lots.",
            risks=[(["nonsense"], "A 7.6% margin.")],
            checks=["Confirm 2026 zoning."],
        )
    )

    assert result == [
        ("unknown_basis", "nonsense"),
        ("unlisted_figure", "$107,560"),
        ("spelled_number", "two"),
        ("unlisted_figure", "7.6%"),
        ("unlisted_figure", "2026"),
    ]


# --- systematic attacks on every figure of the sheet -----------------------------------------


def _figure_mutations() -> list[tuple[str, str]]:
    cases = []
    for key, text in FACTS.figures.items():
        for position, character in enumerate(text):
            if character.isdigit():
                for replacement in "0123456789":
                    if replacement != character:
                        cases.append((key, text[:position] + replacement + text[position + 1 :]))
        for suffix in ("k", "K", "M", "0", "5", ",5", ".5", "x", "%"):
            if not text.endswith("%") or suffix != "%":
                cases.append((key, text + suffix))
        cases.append((key, text[:-1]))
        cases.append((key, text[1:]))
    return cases


def test_no_changed_digit_or_edge_of_any_figure_passes() -> None:
    allowed = set(FACTS.figures.values())
    slipped = []
    for key, mutated in _figure_mutations():
        if mutated in allowed or not any(character.isdigit() for character in mutated):
            continue  # no digit, no number: a figure with its first character cut off is prose
        if check_narrative(draft(f"It is {mutated} now."), FACTS).passed:
            slipped.append((key, mutated))

    assert slipped == []


def test_the_mutation_table_is_large() -> None:
    assert len(_figure_mutations()) > 1000


def test_each_figure_alone_maps_to_its_own_key() -> None:
    for key, text in FACTS.figures.items():
        result = check_narrative(draft(f"The figure is {text}."), FACTS)

        assert result.violations == [], key
        assert key in {f.key for f in result.figures_quoted}


# --- written attacks: one list of strings that must fail, one that must pass -------------------


def test_every_written_attack_is_rejected() -> None:
    from narrative_attacks import ATTACKS

    accepted = [name for name, text in ATTACKS if check_narrative(draft(text), FACTS).passed]

    assert len(ATTACKS) >= 70
    assert accepted == []


def test_every_written_decoy_is_accepted() -> None:
    from narrative_attacks import DECOYS

    rejected = [(name, kinds(draft(text))) for name, text in DECOYS if kinds(draft(text)) != []]

    assert len(DECOYS) >= 15
    assert rejected == []


def test_the_attack_file_stays_ascii() -> None:
    from pathlib import Path

    data = (Path(__file__).parent / "narrative_attacks.py").read_bytes()

    assert all(byte < 128 for byte in data)

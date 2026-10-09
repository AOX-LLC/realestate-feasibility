"""The redactor for listing remarks: every pattern, its false-positive guards, and idempotence."""

import random
import string
import time

import pytest

from feasibility.sources.mls.redact import REMOVED, Redacted, redact_personal


def _clean(text: str) -> str:
    return redact_personal(text).text


# --- the worked example in the plan ---------------------------------------------------------


def test_a_clause_is_removed_and_the_clause_after_the_dash_survives() -> None:
    result = redact_personal(
        "Builder special! Call Dana Whitfield 214.555.0187 or dana at example dot com - sold as-is."
    )

    assert result == Redacted("Builder special! [contact removed] - sold as-is.", 1)


# --- 1. cue-led clauses -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cue",
    [
        "Call",
        "call",
        "Text",
        "Contact",
        "Email",
        "E-mail",
        "Ask for",
        "Reach",
        "Listed by",
        "Listing agent",
        "Agent",
        "Broker",
        "Brokerage",
        "Showings by",
        "Showing with",
        "Showings through",
        "Co-listed with",
        "Co-list by",
        "Presented by",
        "Courtesy of",
        "Represented by",
    ],
)
def test_every_cue_word_removes_its_clause(cue: str) -> None:
    result = redact_personal(f"Corner lot. {cue} Dana Whitfield today. Sold as-is.")

    assert result.text == f"Corner lot. {REMOVED}. Sold as-is."
    assert result.count == 1


def test_the_clause_stops_at_a_spaced_hyphen_so_the_next_clause_is_kept() -> None:
    assert _clean("Call Dana at 214-555-0187 - sold as-is") == f"{REMOVED} - sold as-is"


@pytest.mark.parametrize("dash", ["\u2014", " \u2013 ", " - "])
def test_dashes_end_a_clause(dash: str) -> None:
    assert _clean(f"Call Dana{dash}teardown candidate") == f"{REMOVED}{dash}teardown candidate"


@pytest.mark.parametrize("stop", [".", "!", "?", ";", "\n"])
def test_sentence_ends_stop_a_clause(stop: str) -> None:
    assert _clean(f"Contact Dana{stop} Zoned R-5.") == f"{REMOVED}{stop} Zoned R-5."


def test_a_period_inside_a_number_or_domain_does_not_end_the_clause() -> None:
    assert _clean("Call 214.555.0187 for showings, plans available.") == f"{REMOVED}."


def test_an_honorific_period_does_not_end_the_clause() -> None:
    assert _clean("Contact Mr. Alvarez at the office. Flat lot.") == f"{REMOVED}. Flat lot."


def test_cue_words_match_whole_words_only() -> None:
    text = "Recontact the city; the context is a contacting letter."

    assert _clean(text) == text


# --- 2. emails ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "dana@example.com",
        "dana.whitfield+lot@example.com",
        "dana [at] example [dot] com",
        "dana (at) example (dot) com",
        "dana{at}example.com",
        "dana at example dot com",
        "Dana AT Example DOT com",
        "dana at example.com",
    ],
)
def test_emails_in_every_form_are_removed(email: str) -> None:
    result = redact_personal(f"Plans on request: {email} for a copy.")

    assert result.text == f"Plans on request: {REMOVED} for a copy."
    assert result.count == 1


def test_the_word_at_between_ordinary_words_is_not_an_email() -> None:
    text = "Meet the builder at the lot and walk it at noon."

    assert _clean(text) == text


# --- 3. phone numbers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone",
    [
        "214-555-0187",
        "(214) 555-0187",
        "214.555.0187",
        "2145550187",
        "+1 214 555 0187",
        "1-214-555-0187",
    ],
)
def test_phone_numbers_in_five_formats_are_removed(phone: str) -> None:
    result = redact_personal(f"Plans are ready, {phone}, and the survey is current.")

    assert result.text == f"Plans are ready, {REMOVED}, and the survey is current."


def test_a_seven_digit_number_is_removed_after_a_cue_word() -> None:
    assert _clean("Call 555-0187 anytime") == REMOVED


def test_money_lot_sizes_and_years_are_not_phone_numbers() -> None:
    text = "Priced at $1,250,000. Lot is 7,500 sq ft, built 1962, zoned R-7.5(A)."

    assert _clean(text) == text


# --- 4. URLs and domains -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        "https://listings.example.com/lot-9",
        "http://example.com",
        "www.example.com/plans",
        "example.com",
        "tour.example.net/abc?x=1",
    ],
)
def test_urls_and_bare_domains_are_removed(link: str) -> None:
    result = redact_personal(f"Virtual tour {link} anytime.")

    assert result.text == f"Virtual tour {REMOVED} anytime."


def test_a_trailing_full_stop_is_not_swallowed_by_the_url() -> None:
    assert _clean("Plans at www.example.com/plans.") == f"Plans at {REMOVED}."


def test_measurements_with_dots_are_not_domains() -> None:
    text = "Lot approx 50 x 150 sq.ft. with 2.5 baths, e.g. a pool."

    assert _clean(text) == text


# --- 5. honorific plus name --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Mr. Alvarez", "Mrs Patel", "Ms. Okafor-Reyes", "Dr. Lindgren", "Miss Hartwell", "Mx. Quill"],
)
def test_honorific_names_are_removed(name: str) -> None:
    result = redact_personal(f"Seller {name} will review offers.")

    assert result.text == f"Seller {REMOVED} will review offers."


def test_drive_written_dr_without_a_period_is_a_street_not_a_doctor() -> None:
    text = "Corner of Elm Dr Lot 4 near the park."

    assert _clean(text) == text


# --- 6. brokerage names ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Whitfield Realty",
        "Lone Star Realtors",
        "Prairie Real Estate Group",
        "Northpark Brokerage",
        "Hartwell & Quill Properties",
        "Bluebonnet Real Estate",
    ],
)
def test_brokerage_names_are_removed(name: str) -> None:
    result = redact_personal(f"Offered by {name} exclusively.")

    assert result.text == f"Offered by {REMOVED} exclusively."


@pytest.mark.parametrize(
    "text",
    [
        "Real Estate taxes are low here.",
        "Properties of the lot include a creek.",
        "The Dallas Real Estate taxes were paid in full.",
        "Close to the Properties of interest.",
    ],
)
def test_ordinary_uses_of_the_brokerage_words_stay(text: str) -> None:
    assert _clean(text) == text


# --- 7. licence numbers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "licence",
    ["TREC #0123456", "TREC 0123456", "License 4567890", "License # 4567890", "Lic. No. 4567890"],
)
def test_license_numbers_are_removed(licence: str) -> None:
    result = redact_personal(f"Seller is licensed, {licence}, and disclosed.")

    assert result.text == f"Seller is licensed, {REMOVED}, and disclosed."


def test_the_word_license_without_a_number_stays() -> None:
    text = "Building license required for demolition."

    assert _clean(text) == text


# --- count, structure and the cases that must not change --------------------------------------


def test_the_count_is_the_number_of_replacements() -> None:
    result = redact_personal("Visit www.example.com. Dr. Lindgren owns it. Plans are ready.")

    assert result.count == 2
    assert result.text == f"Visit {REMOVED}. {REMOVED} owns it. Plans are ready."


def test_text_without_personal_data_is_returned_unchanged_with_count_zero() -> None:
    text = "Teardown candidate on a flat corner lot. Mature oaks. Survey available."

    assert redact_personal(text) == Redacted(text, 0)


def test_an_empty_string_is_empty() -> None:
    assert redact_personal("") == Redacted("", 0)


def test_the_replacement_token_does_not_trigger_the_contact_cue() -> None:
    once = redact_personal("Builder special! Call Dana 214-555-0187 - sold as-is.")

    assert redact_personal(once.text) == Redacted(once.text, 0)


# --- idempotence -------------------------------------------------------------------------------

CORPUS = [
    "Builder special! Call Dana Whitfield 214.555.0187 or dana at example dot com - sold as-is.",
    "Listed by Prairie Real Estate Group, TREC #0123456. See www.example.com/lot.",
    "Mr. Alvarez at (214) 555-0187; email dana [at] example [dot] com. Flat lot.",
    "Contact: Dana. Contact the city about the easement. Reach out, text 555-0187.",
    "Dr. Lindgren, Hartwell & Quill Properties, +1 214 555 0187, tour.example.net/a",
]


@pytest.mark.parametrize("text", CORPUS)
def test_redacting_twice_changes_nothing(text: str) -> None:
    once = redact_personal(text)

    assert redact_personal(once.text).text == once.text


def test_redacting_twice_changes_nothing_on_random_text() -> None:
    rng = random.Random(4242)
    words = [
        "call",
        "Dana",
        "Mr.",
        "at",
        "dot",
        "com",
        "example",
        "214-555-0187",
        "(214)",
        "555-0187",
        "Realty",
        "Real Estate",
        "contact",
        "[contact removed]",
        "-",
        "\u2014",
        ".",
        "!",
        ";",
        "www.example.com",
        "dana@example.com",
        "License",
        "#",
        "0123456",
        "lot",
        "sold",
        "as-is",
        "Properties",
        "Group",
    ]
    for _ in range(400):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 25)))
        once = redact_personal(text).text
        assert redact_personal(once).text == once, text


@pytest.mark.parametrize(
    "shape",
    [
        "a" * 40_000,
        "a." * 20_000,
        "a-" * 20_000,
        "a " * 20_000,
        "Ab " * 13_000,
        "1" * 40_000,
        "a at " * 8_000,
        "a [" * 13_000,
        "call " * 8_000,
        "Mr " * 13_000,
        "( " * 20_000,
    ],
)
def test_the_redactor_is_linear_on_hostile_input(shape: str) -> None:
    """A run of word characters with no "@" once cost a scan from every character in it:
    40,000 characters took over a minute. The budget is generous; the quadratic cases are
    orders of magnitude beyond it."""
    started = time.perf_counter()
    redact_personal(shape)

    assert time.perf_counter() - started < 3.0


@pytest.mark.parametrize(
    "phone",
    [
        "214 - 555 - 0187",
        "214/555/0187",
        "214\u2013555\u20130187",
        "214  555  0187",
        "(214)555-0187",
    ],
)
def test_phone_numbers_with_loose_separators_are_removed(phone: str) -> None:
    assert (
        _clean(f"Plans ready, {phone}, survey current.")
        == f"Plans ready, {REMOVED}, survey current."
    )


def test_full_width_digits_cannot_hide_a_number() -> None:
    assert _clean(
        "Plans ready, \uff12\uff11\uff14-\uff15\uff15\uff15-\uff10\uff11\uff18\uff17."
    ) == (f"Plans ready, {REMOVED}.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Seller MR. ALVAREZ will review.", f"Seller {REMOVED} will review."),
        ("Seller mr. Alvarez will review.", f"Seller {REMOVED} will review."),
        ("Offered by WHITFIELD REALTY exclusively.", f"Offered by {REMOVED} exclusively."),
        ("Offered by Whitfield REALTY exclusively.", f"Offered by {REMOVED} exclusively."),
        ("Mail dana @ example.com ok.", f"Mail {REMOVED} ok."),
        ("Write dana@example ok.", f"Write {REMOVED} ok."),
        ("On IG @whitfieldhomes today.", f"On {REMOVED} today."),
        ("Plans: dana at example . com ok.", f"Plans: {REMOVED} ok."),
    ],
)
def test_capitals_spacing_and_handles_do_not_hide_personal_data(text: str, expected: str) -> None:
    assert _clean(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Questions? Maria will meet you there.",
        "Plans: dana_at_example_dot_com",
        "five five five oh one eight seven",
    ],
)
def test_the_known_residual_forms_are_left_alone_and_stated(text: str) -> None:
    """Pattern-based redaction cannot catch these. The residual eval set measures them and
    docs/ARCHITECTURE.md states the gap; this test pins today's behaviour so a change to it
    is deliberate."""
    assert _clean(text) == text


def test_the_corpus_text_has_no_printable_leftovers_of_a_planted_contact() -> None:
    result = _clean(CORPUS[0] + " " + CORPUS[1] + " " + CORPUS[4])

    for leftover in ("Dana", "0187", "example", "Prairie", "Lindgren", "Hartwell"):
        assert leftover not in result
    assert set(result) <= set(string.printable)

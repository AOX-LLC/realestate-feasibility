"""The committed synthetic RESO records: what they cover, that they are fictional, and that the
redactor and the injection scan treat them as the data authors intended.

The tests take no fixtures and touch no database, so a plain script can call them without
pytest while the data is being written."""

import re
from collections import Counter

from mls_data import (
    INJECTED_SNAPSHOT,
    NEGATIONS_ONLY,
    NULL_REMARKS,
    all_records,
    answer_key,
    decode_tag_characters,
    extra_records,
    feed_listings,
    ingested,
    redacted_text,
    snapshot_records,
)

from feasibility.llm.untrusted import HIDDEN_TEXT_MARKER, overlaps, scan_injection
from feasibility.sources.mls.reso import DROPPED_FIELDS, ResoProperty

REQUIRED_FIELDS = {
    "ListingKey",
    "ListingId",
    "UnparsedAddress",
    "PostalCode",
    "ListPrice",
    "StandardStatus",
    "PublicRemarks",
    *DROPPED_FIELDS,
}
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b")
FICTIONAL_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]*555[\s.-]*01\d\d\b")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _by_id(records: list[dict]) -> dict[str, dict]:
    return {record["ListingId"]: record for record in records}


# --- shape and coverage ------------------------------------------------------------------------


def test_every_record_has_every_field_and_validates() -> None:
    for record in all_records():
        assert record.keys() >= REQUIRED_FIELDS, record["ListingId"]
        ResoProperty.model_validate(record)


def test_every_distinct_mls_number_in_both_feeds_has_exactly_one_record() -> None:
    feed = set(feed_listings())
    ids = [record["ListingId"] for record in snapshot_records()]

    assert sorted(ids) == sorted(set(ids)), "duplicate ListingId"
    assert set(ids) == feed


def test_no_record_shares_an_id_with_another_file_or_lacks_a_feed_listing() -> None:
    feed = set(feed_listings())
    extra_ids = {record["ListingId"] for record in extra_records()}

    assert extra_ids.isdisjoint(feed)
    assert extra_ids.isdisjoint(record["ListingId"] for record in snapshot_records())
    assert len(extra_ids) == len(extra_records())


def test_snapshot_records_agree_with_their_feed_listing() -> None:
    feed = feed_listings()
    for record in snapshot_records():
        listing = feed[record["ListingId"]]
        assert record["UnparsedAddress"] == listing["formattedAddress"], record["ListingId"]
        assert record["PostalCode"] == listing["zipCode"], record["ListingId"]
        assert float(record["ListPrice"]) == float(listing["price"]), record["ListingId"]
        assert record["StandardStatus"] == "Active"


def test_listing_keys_are_unique_across_both_files() -> None:
    keys = [record["ListingKey"] for record in all_records()]

    assert len(keys) == len(set(keys))


def test_the_named_records_have_no_remarks_and_everything_else_has_some() -> None:
    by_id = _by_id(snapshot_records())

    assert {i for i, r in by_id.items() if r["PublicRemarks"] is None} == NULL_REMARKS
    assert all(r["PublicRemarks"] for r in extra_records())
    assert all(ingested(r) is not None for r in all_records() if r["PublicRemarks"] is not None)


def test_the_counts_the_plan_asks_for() -> None:
    assert 25 <= len(snapshot_records()) <= 32
    assert 25 <= len(extra_records()) <= 36


# --- everything personal in a record is fictional ----------------------------------------------


def test_the_dropped_fields_hold_obviously_fictional_values() -> None:
    for record in all_records():
        for field in ("ListAgentDirectPhone", "ListOfficePhone"):
            assert FICTIONAL_PHONE.search(record[field]), (record["ListingId"], field)
        assert record["ListAgentEmail"].endswith("@example.com"), record["ListingId"]
        assert record["ListAgentFullName"].strip() and record["ListOfficeName"].strip()
        assert record["PrivateRemarks"].strip() and record["ShowingInstructions"].strip()


def test_every_phone_and_email_in_any_remarks_is_fictional() -> None:
    for record in all_records():
        raw = record["PublicRemarks"] or ""
        for phone in PHONE.findall(raw):
            assert FICTIONAL_PHONE.search(phone), (record["ListingId"], phone)
        for email in EMAIL.findall(raw):
            assert email.endswith("@example.com"), (record["ListingId"], email)


def test_the_private_fields_never_leak_into_public_remarks() -> None:
    for record in all_records():
        raw = record["PublicRemarks"] or ""
        assert record["PrivateRemarks"] not in raw
        assert record["ShowingInstructions"] not in raw


# --- planted personal data ---------------------------------------------------------------------


def _planted(key: dict[str, dict]) -> dict[str, list[str]]:
    return {i: entry["planted_personal"] for i, entry in key.items() if entry["planted_personal"]}


def test_every_non_residual_planted_string_is_in_the_raw_and_gone_from_the_redacted_text() -> None:

    key = answer_key()
    records = _by_id(all_records())
    for listing_id, strings in _planted(key).items():
        if "personal:residual" in key[listing_id]["tags"]:
            continue
        raw = records[listing_id]["PublicRemarks"]
        redacted = redacted_text(records[listing_id])
        for planted in strings:
            assert planted in raw, (listing_id, planted, "not in the raw remarks")
            assert planted not in redacted, (listing_id, planted, "survived redaction")


def test_at_least_ten_records_carry_planted_personal_data() -> None:

    key = answer_key()
    planted = [
        i
        for i, entry in key.items()
        if entry["planted_personal"] and "personal:residual" not in entry["tags"]
    ]

    assert len(planted) >= 10


def test_the_planted_set_covers_every_form_the_redactor_targets() -> None:

    key = answer_key()
    records = _by_id(all_records())
    planted_raw = " \n ".join(
        records[i]["PublicRemarks"]
        for i, entry in key.items()
        if entry["planted_personal"] and "personal:residual" not in entry["tags"]
    )
    phone_formats = {
        "dashed": r"\b\d{3}-555-01\d\d\b",
        "parenthesised": r"\(\d{3}\) 555-01\d\d",
        "dotted": r"\b\d{3}\.555\.01\d\d\b",
        "bare": r"(?<![\d-])\d{3}555\d{4}\b",
        "international": r"\+1 \d{3} 555 01\d\d",
    }
    forms = {
        **{f"phone {name}": pattern for name, pattern in phone_formats.items()},
        "cue name": (
            r"(?i)\b(?:call|text|contact|ask for|listed by|presented by|courtesy of)\s+[A-Z]"
        ),
        "honorific": r"\b(?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z][a-z]+",
        "email": r"[\w.+-]+@example\.com",
        "spelled-out email": r"(?i)\b\w+\s+at\s+example\s+dot\s+com\b",
        "brokerage": (
            r"\b[A-Z]\w+(?:\s+[A-Z&]\w*)*\s+"
            r"(?:Realty|Realtors?|Real Estate(?: Group)?|Properties)\b"
        ),
        "url": r"(?i)\b(?:https?://|www\.)\S+",
        "license": r"\b(?:TREC|License)\b[^.\n]{0,12}\d{5,}",
    }
    missing = [name for name, pattern in forms.items() if not re.search(pattern, planted_raw)]

    assert missing == []


def test_the_residual_set_is_eval_only_and_has_at_least_three_records() -> None:

    key = answer_key()
    residual = [i for i, entry in key.items() if "personal:residual" in entry["tags"]]
    extra_ids = {record["ListingId"] for record in extra_records()}

    assert len(residual) >= 3
    assert set(residual) <= extra_ids
    for listing_id in residual:
        assert key[listing_id]["planted_personal"], listing_id


# --- injection ---------------------------------------------------------------------------------


def _injections() -> dict[str, dict]:

    return {i: e["injection"] for i, e in answer_key().items() if e["injection"] is not None}


def test_at_least_eight_records_carry_an_injection_with_the_plans_split() -> None:

    injected = set(_injections())
    extra_ids = {record["ListingId"] for record in extra_records()}

    assert len(injected) >= 8
    assert len(injected & extra_ids) >= 6
    assert injected >= INJECTED_SNAPSHOT
    assert injected - extra_ids == INJECTED_SNAPSHOT


def test_each_canary_is_unique_and_is_in_the_raw_remarks_as_a_reader_would_see_it() -> None:
    records = _by_id(all_records())
    canaries = Counter(entry["canary"] for entry in _injections().values())

    assert all(count == 1 for count in canaries.values())
    for listing_id, injection in _injections().items():
        raw = records[listing_id]["PublicRemarks"]
        assert injection["canary"] in raw + decode_tag_characters(raw), listing_id


def test_a_canary_is_never_a_word_an_ordinary_remark_uses() -> None:
    for injection in _injections().values():
        assert re.fullmatch(r"[A-Z][A-Z0-9]{5,}", injection["canary"]), injection["canary"]


def test_each_injected_span_is_in_the_stored_remarks() -> None:
    records = _by_id(all_records())
    for listing_id, injection in _injections().items():
        stored = redacted_text(records[listing_id])
        assert injection["span"] in stored, (listing_id, injection["span"])


def test_the_scan_flags_every_injected_span_and_no_clean_remark() -> None:
    records = _by_id(all_records())
    injected = _injections()
    for listing_id, record in records.items():
        stored = redacted_text(record)
        hits = scan_injection(stored)
        if listing_id in injected:
            span = injected[listing_id]["span"]
            start = stored.index(span)
            assert overlaps(start, start + len(span), hits), (listing_id, "scan missed it")
        else:
            assert hits == [], (listing_id, [h.rule for h in hits])


def test_the_scan_rules_between_them_see_every_kind_of_injection_in_the_set() -> None:
    records = _by_id(all_records())
    rules: set[str] = set()
    for listing_id in _injections():
        rules |= {hit.rule for hit in scan_injection(redacted_text(records[listing_id]))}

    assert {
        "ignore_instructions",
        "system_marker",
        "output_directive",
        "fence_tag",
        "hidden_text",
    } <= rules


def test_a_hidden_text_injection_is_stored_as_the_marker_alone() -> None:
    hidden = [
        listing_id
        for listing_id, record in _by_id(all_records()).items()
        if decode_tag_characters(record["PublicRemarks"] or "")
    ]

    assert hidden, "no record hides its instructions in the tag block"
    for listing_id in hidden:
        stored = redacted_text(_by_id(all_records())[listing_id])
        assert HIDDEN_TEXT_MARKER in stored
        assert _injections()[listing_id]["canary"] not in stored


def test_the_negations_only_snapshot_record_is_injected_and_expects_nothing() -> None:

    entry = answer_key()[NEGATIONS_ONLY]

    assert entry["signals"] == []
    assert entry["injection"] is not None
    assert "negations-only" in entry["tags"]

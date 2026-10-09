"""The answer key for the extraction eval: its shape, and that it agrees with the records.

The key is written and reviewed before anything is recorded and is not edited afterwards to
raise a score. These tests take no fixtures and touch no database."""

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from mls_data import (
    CATALOGUE,
    DEMO_SIGNALS,
    KEY_FILE,
    NULL_REMARKS,
    all_records,
    answer_key,
    extra_records,
    redacted_text,
    snapshot_records,
)

HARD_NEGATIVE_TAG = re.compile(r"^hardneg:([a-z_]+)$")
INJECTION_TAG = re.compile(
    r"^injection:(ignore_previous|fake_system|close_tag|hidden_text|report_all|fake_quote"
    r"|plant_number)$"
)
OTHER_TAGS = {"personal:planted", "personal:residual", "negations-only", "demo", "null-remarks"}
ENTRY_KEYS = {"signals", "planted_personal", "injection", "split", "tags"}
MIN_QUOTE, MAX_QUOTE = 8, 200


def _entries() -> dict[str, dict[str, Any]]:
    return answer_key()


def _records() -> dict[str, dict[str, Any]]:
    return {record["ListingId"]: record for record in all_records()}


def _positives(entries: dict[str, dict[str, Any]], code: str, split: str | None) -> int:
    return sum(
        1
        for entry in entries.values()
        if (split is None or entry["split"] == split)
        and code in {signal["code"] for signal in entry["signals"]}
    )


def _hard_negatives(entries: dict[str, dict[str, Any]], code: str, split: str | None) -> int:
    return sum(
        1
        for entry in entries.values()
        if (split is None or entry["split"] == split) and f"hardneg:{code}" in entry["tags"]
    )


# --- shape -------------------------------------------------------------------------------------


def test_the_key_has_exactly_one_entry_per_record() -> None:
    assert set(_entries()) == set(_records())


def test_every_entry_has_the_documented_shape() -> None:
    for listing_id, entry in _entries().items():
        assert set(entry) == ENTRY_KEYS, listing_id
        assert entry["split"] in {"dev", "holdout"}, listing_id
        assert isinstance(entry["tags"], list), listing_id
        assert all(isinstance(s, str) and s for s in entry["planted_personal"]), listing_id
        for signal in entry["signals"]:
            assert set(signal) == {"code", "evidence"}, listing_id
        injection = entry["injection"]
        assert injection is None or set(injection) == {"canary", "span"}, listing_id


def test_every_tag_is_one_the_tests_understand() -> None:
    for listing_id, entry in _entries().items():
        for tag in entry["tags"]:
            known = HARD_NEGATIVE_TAG.match(tag) or INJECTION_TAG.match(tag) or tag in OTHER_TAGS
            assert known, (listing_id, tag)
            match = HARD_NEGATIVE_TAG.match(tag)
            if match:
                assert match.group(1) in CATALOGUE, (listing_id, tag)


def test_tags_agree_with_the_fields_they_describe() -> None:
    for listing_id, entry in _entries().items():
        tags = set(entry["tags"])
        has_injection_tag = any(INJECTION_TAG.match(tag) for tag in tags)
        assert has_injection_tag == (entry["injection"] is not None), listing_id
        planted = bool(entry["planted_personal"])
        assert planted == bool(tags & {"personal:planted", "personal:residual"}), listing_id
        assert not {"personal:planted", "personal:residual"} <= tags, listing_id


def test_every_signal_code_is_in_the_catalogue_and_appears_once_per_record() -> None:
    for listing_id, entry in _entries().items():
        codes = [signal["code"] for signal in entry["signals"]]
        assert set(codes) <= set(CATALOGUE), (listing_id, set(codes) - set(CATALOGUE))
        assert len(codes) == len(set(codes)), listing_id


# --- evidence ----------------------------------------------------------------------------------


def test_every_evidence_string_is_verbatim_in_the_redacted_remarks() -> None:
    records = _records()
    for listing_id, entry in _entries().items():
        stored = redacted_text(records[listing_id])
        for signal in entry["signals"]:
            evidence = signal["evidence"]
            assert evidence in stored, (listing_id, signal["code"], evidence)


def test_evidence_is_a_quotable_length_and_contains_no_redaction_token() -> None:
    for listing_id, entry in _entries().items():
        for signal in entry["signals"]:
            evidence = signal["evidence"]
            assert MIN_QUOTE <= len(evidence) <= MAX_QUOTE, (listing_id, evidence)
            assert "[contact removed]" not in evidence, (listing_id, evidence)


def test_evidence_never_overlaps_the_injected_span() -> None:
    records = _records()
    for listing_id, entry in _entries().items():
        injection = entry["injection"]
        if injection is None:
            continue
        stored = redacted_text(records[listing_id])
        span_start = stored.index(injection["span"])
        span_end = span_start + len(injection["span"])
        for signal in entry["signals"]:
            start = stored.index(signal["evidence"])
            overlap = start < span_end and span_start < start + len(signal["evidence"])
            assert not overlap, (listing_id, signal["code"])


# --- named records -----------------------------------------------------------------------------


def test_the_named_snapshot_candidates_carry_exactly_the_planned_signals() -> None:
    entries = _entries()
    for listing_id, expected in DEMO_SIGNALS.items():
        assert {s["code"] for s in entries[listing_id]["signals"]} == expected, listing_id


def test_records_without_remarks_expect_nothing() -> None:
    entries = _entries()
    for listing_id in NULL_REMARKS:
        assert entries[listing_id]["signals"] == []
        assert entries[listing_id]["planted_personal"] == []
        assert entries[listing_id]["injection"] is None


def test_the_snapshot_has_a_record_that_expects_nothing_besides_the_null_ones() -> None:
    entries = _entries()
    snapshot_ids = {record["ListingId"] for record in snapshot_records()}
    quiet = {i for i in snapshot_ids - NULL_REMARKS if entries[i]["signals"] == []}

    assert quiet, "no snapshot record with remarks expects zero signals"
    assert all("negations-only" in entries[i]["tags"] or entries[i]["tags"] for i in quiet)


# --- coverage of the catalogue -----------------------------------------------------------------


def test_every_signal_has_at_least_four_positives_and_two_hard_negatives() -> None:
    entries = _entries()
    short = {
        code: (_positives(entries, code, None), _hard_negatives(entries, code, None))
        for code in CATALOGUE
        if _positives(entries, code, None) < 4 or _hard_negatives(entries, code, None) < 2
    }

    assert short == {}


def test_every_signal_has_at_least_two_of_each_in_the_holdout() -> None:
    entries = _entries()
    short = {
        code: (_positives(entries, code, "holdout"), _hard_negatives(entries, code, "holdout"))
        for code in CATALOGUE
        if _positives(entries, code, "holdout") < 2 or _hard_negatives(entries, code, "holdout") < 2
    }

    assert short == {}


def test_a_hard_negative_for_a_code_does_not_also_expect_that_code() -> None:
    for listing_id, entry in _entries().items():
        expected = {signal["code"] for signal in entry["signals"]}
        for tag in entry["tags"]:
            match = HARD_NEGATIVE_TAG.match(tag)
            if match:
                assert match.group(1) not in expected, (listing_id, tag)


def test_the_eval_only_set_splits_between_dev_and_holdout() -> None:
    entries = _entries()
    splits = Counter(entries[record["ListingId"]]["split"] for record in extra_records())
    by_split = defaultdict(int)
    for entry in entries.values():
        by_split[entry["split"]] += 1

    assert splits["dev"] >= 8 and splits["holdout"] >= 8
    assert by_split["dev"] >= 20 and by_split["holdout"] >= 20


# The key is frozen once it has been blind-labelled and before anything is recorded. Changing
# a label, its evidence or its split to improve a score would defeat the eval, so a change here
# is a decision for the project owner and costs a new recording.
FROZEN_LABELS_SHA256 = "e999b84409bbd413ef65435a2d91a1ff9258e38c3f4cab471ecc5db2452cb5db"


def test_the_answer_key_is_frozen() -> None:
    digest = hashlib.sha256(KEY_FILE.read_bytes()).hexdigest()

    assert digest == FROZEN_LABELS_SHA256

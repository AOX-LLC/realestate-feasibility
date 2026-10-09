"""The stored shape of a candidate's signals (`SignalsResult` v1): Phase 5 reads these keys."""

import re
from typing import Any

import pytest
from pydantic import ValidationError

from feasibility.llm.field_signals import FieldSignal
from feasibility.llm.results import (
    ExtractionInfo,
    RemarksInfo,
    SignalsResult,
    StoredSignal,
)
from feasibility.llm.signals import DroppedClaim, VerifiedSignal

PERSONAL_DATA_NAME = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal")

REMARKS = RemarksInfo(
    sha256="a" * 64,
    char_count=212,
    redaction_count=1,
    removed_invisible_count=0,
    suspicious=False,
    suspicious_rules=[],
)
EXTRACTION = ExtractionInfo(
    prompt_id="signals.extract",
    prompt_version=1,
    tier="small",
    input_sha256="b" * 64,
    reused=False,
    llm_call_id=7,
)
REMARKS_SIGNAL = StoredSignal.from_verified(
    VerifiedSignal(code="as_is_sale", polarity="risk", quote="Sold as-is, seller makes no repairs")
)
FIELD_SIGNAL = StoredSignal.from_field(
    FieldSignal("price_reduced", "opportunity", "price", "500000.00 to 479000.00")
)


def extracted(**changes: Any) -> SignalsResult:
    fields: dict[str, Any] = {
        "status": "extracted",
        "reason": None,
        "remarks": REMARKS,
        "signals": [REMARKS_SIGNAL, FIELD_SIGNAL],
        "dropped": [DroppedClaim(code="protected_trees", reason="quote_not_found")],
        "model_flagged_injection": False,
        "extraction": EXTRACTION,
    }
    fields.update(changes)
    return SignalsResult(**fields)


def test_a_result_round_trips_through_json_unchanged() -> None:
    result = extracted()

    assert SignalsResult.model_validate(result.model_dump(mode="json")) == result


def test_the_stored_keys_are_the_documented_ones() -> None:
    document = extracted().model_dump(mode="json")

    assert set(document) == {
        "version",
        "status",
        "reason",
        "remarks",
        "signals",
        "dropped",
        "model_flagged_injection",
        "extraction",
    }
    assert document["version"] == 1
    assert set(document["signals"][0]) == {
        "code",
        "polarity",
        "source",
        "quote",
        "field",
        "field_value",
    }
    assert set(document["remarks"]) == {
        "sha256",
        "char_count",
        "redaction_count",
        "removed_invisible_count",
        "suspicious",
        "suspicious_rules",
    }
    assert set(document["extraction"]) == {
        "prompt_id",
        "prompt_version",
        "tier",
        "input_sha256",
        "reused",
        "llm_call_id",
    }


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(key for item in value.values() for key in _keys(item))]
    if isinstance(value, list):
        return [key for item in value for key in _keys(item)]
    return []


def test_no_stored_key_matches_the_personal_data_name_pattern() -> None:
    keys = _keys(extracted().model_dump(mode="json"))

    assert [key for key in keys if PERSONAL_DATA_NAME.search(key)] == []


def test_a_remarks_signal_carries_a_quote_and_no_field() -> None:
    assert (REMARKS_SIGNAL.source, REMARKS_SIGNAL.field, REMARKS_SIGNAL.field_value) == (
        "remarks",
        None,
        None,
    )
    assert REMARKS_SIGNAL.quote == "Sold as-is, seller makes no repairs"


def test_a_field_signal_carries_a_field_and_a_value_and_no_quote() -> None:
    assert FIELD_SIGNAL.source == "fields"
    assert FIELD_SIGNAL.quote is None
    assert (FIELD_SIGNAL.field, FIELD_SIGNAL.field_value) == ("price", "500000.00 to 479000.00")


def test_a_signal_cannot_mix_a_quote_with_a_field() -> None:
    with pytest.raises(ValidationError, match="quote"):
        StoredSignal(
            code="as_is_sale",
            polarity="risk",
            source="fields",
            quote="Sold as-is",
            field="price",
            field_value="1",
        )
    with pytest.raises(ValidationError, match="field"):
        StoredSignal(
            code="as_is_sale",
            polarity="risk",
            source="remarks",
            quote="Sold as-is, no repairs",
            field="price",
            field_value="1",
        )


def test_a_remarks_signal_needs_a_quote() -> None:
    with pytest.raises(ValidationError, match="quote"):
        StoredSignal(code="as_is_sale", polarity="risk", source="remarks")


def test_a_code_outside_both_catalogues_is_refused() -> None:
    with pytest.raises(ValidationError):
        StoredSignal(
            code="owner_motivated", polarity="risk", source="fields", field="a", field_value="b"
        )  # type: ignore[arg-type]


def test_a_reason_goes_with_every_status_but_extracted() -> None:
    with pytest.raises(ValidationError, match="reason"):
        extracted(reason="budget")
    with pytest.raises(ValidationError, match="reason"):
        SignalsResult(status="fields_only", reason=None, signals=[FIELD_SIGNAL])


def test_fields_only_with_no_remarks_is_a_valid_result() -> None:
    result = SignalsResult(status="fields_only", reason="no_remarks", signals=[FIELD_SIGNAL])

    assert result.remarks is None
    assert result.extraction is None
    assert result.model_flagged_injection is None


def test_only_an_extracted_result_may_hold_remarks_signals_or_a_model_verdict() -> None:
    with pytest.raises(ValidationError, match="remarks"):
        SignalsResult(status="deferred", reason="budget", signals=[REMARKS_SIGNAL])
    with pytest.raises(ValidationError, match="model_flagged_injection"):
        SignalsResult(status="failed", reason="refusal", signals=[], model_flagged_injection=True)


def test_an_extracted_result_needs_its_remarks_and_extraction_records() -> None:
    with pytest.raises(ValidationError, match="remarks"):
        extracted(remarks=None)
    with pytest.raises(ValidationError, match="extraction"):
        extracted(extraction=None)
    with pytest.raises(ValidationError, match="model_flagged_injection"):
        extracted(model_flagged_injection=None)


def test_a_stored_result_rejects_unknown_keys_and_other_versions() -> None:
    document = extracted().model_dump(mode="json")

    with pytest.raises(ValidationError):
        SignalsResult.model_validate({**document, "surprise": 1})
    with pytest.raises(ValidationError):
        SignalsResult.model_validate({**document, "version": 2})


def test_a_dropped_claim_stores_no_quote() -> None:
    document = extracted().model_dump(mode="json")

    assert document["dropped"] == [{"code": "protected_trees", "reason": "quote_not_found"}]


def test_hashes_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        RemarksInfo(
            sha256="not-a-hash",
            char_count=1,
            redaction_count=0,
            removed_invisible_count=0,
            suspicious=False,
            suspicious_rules=[],
        )


def test_a_cached_extraction_holds_remarks_signals_only() -> None:
    from pydantic import ValidationError

    from feasibility.llm.results import CachedExtraction, StoredSignal

    quote = StoredSignal(
        code="as_is_sale", polarity="risk", source="remarks", quote="Sold as-is....."
    )
    field = StoredSignal(
        code="relisted",
        polarity="risk",
        source="fields",
        field="change_kind",
        field_value="relisted",
    )
    digest = "c" * 64

    kept = CachedExtraction(
        signals=[quote], dropped=[], model_flagged_injection=False, scan_fingerprint=digest
    )
    assert CachedExtraction.model_validate(kept.model_dump(mode="json")) == kept
    with pytest.raises(ValidationError, match="remarks signals only"):
        CachedExtraction(
            signals=[field], dropped=[], model_flagged_injection=False, scan_fingerprint=digest
        )

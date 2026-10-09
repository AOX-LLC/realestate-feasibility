"""What is stored for a candidate: the shapes Phase 5 reads, frozen after the recordings land.

Every model is frozen, rejects unknown keys and carries `version`. They are stored with
`model_dump(mode="json")`. A model the LLM fills (`SignalExtraction`, `NarrativeDraft`) has no
numeric field; the models here are what code writes after verifying the model's output, so they
may hold counts and ids.

No key may match the personal-data name pattern (`owner|mail|phone|email|agent|office|taxpayer|
legal`) that the schema and API tests apply, so no field below is named for one.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from feasibility.llm.catalogue import Polarity, SignalCode
from feasibility.llm.field_signals import FieldSignal, FieldSignalCode
from feasibility.llm.signals import DroppedClaim, VerifiedSignal

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- signals ------------------------------------------------------------------------------------

SignalsStatus = Literal["extracted", "fields_only", "failed", "deferred"]
SignalsReason = Literal[
    "no_remarks",
    "llm_not_configured",
    "budget",
    "provider_error",
    "structured_error",
    "refusal",
    "replay_error",
]
SignalSource = Literal["remarks", "fields"]


class RemarksInfo(ResultModel):
    """The remarks that were read, by hash and size; never their text."""

    sha256: Sha256Hex
    char_count: Annotated[int, Field(ge=0)]
    redaction_count: Annotated[int, Field(ge=0)]
    removed_invisible_count: Annotated[int, Field(ge=0)]
    suspicious: bool
    suspicious_rules: list[str]


class StoredSignal(ResultModel):
    """One signal that held: a verified quote from the remarks, or a field and its value."""

    code: SignalCode | FieldSignalCode
    polarity: Polarity
    source: SignalSource
    quote: str | None = None
    field: str | None = None
    field_value: str | None = None

    @model_validator(mode="after")
    def _evidence_matches_the_source(self) -> Self:
        if self.source == "remarks":
            if not self.quote:
                raise ValueError("a remarks signal needs its quote")
            if self.field is not None or self.field_value is not None:
                raise ValueError("a remarks signal has a quote, not a field")
        else:
            if self.quote is not None:
                raise ValueError("a field signal has a field, not a quote")
            if not self.field or self.field_value is None:
                raise ValueError("a field signal needs its field and its value")
        return self

    @classmethod
    def from_verified(cls, signal: VerifiedSignal) -> Self:
        return cls(code=signal.code, polarity=signal.polarity, source="remarks", quote=signal.quote)

    @classmethod
    def from_field(cls, signal: FieldSignal) -> Self:
        return cls(
            code=signal.code,
            polarity=signal.polarity,
            source="fields",
            field=signal.field,
            field_value=signal.field_value,
        )


class ExtractionInfo(ResultModel):
    """Which prompt and input produced the verified signals, and whether a call was spent."""

    prompt_id: str
    prompt_version: Annotated[int, Field(ge=1)]
    tier: str
    input_sha256: Sha256Hex
    reused: bool
    llm_call_id: int | None


class SignalsResult(ResultModel):
    """`candidate_signals.result`. Field signals appear in every status."""

    version: Literal[1] = 1
    status: SignalsStatus
    reason: SignalsReason | None
    remarks: RemarksInfo | None = None
    signals: list[StoredSignal]
    dropped: list[DroppedClaim] = Field(default_factory=list)
    model_flagged_injection: bool | None = None
    extraction: ExtractionInfo | None = None

    @model_validator(mode="after")
    def _the_status_decides_what_is_present(self) -> Self:
        if (self.status == "extracted") != (self.reason is None):
            raise ValueError("a reason is given for every status except extracted, and only then")
        if self.status != "extracted":
            if any(signal.source == "remarks" for signal in self.signals):
                raise ValueError("only an extracted result holds remarks signals")
            if self.model_flagged_injection is not None or self.dropped:
                raise ValueError(
                    "only an extracted result holds model_flagged_injection or dropped claims"
                )
            return self
        if self.remarks is None or self.extraction is None:
            raise ValueError("an extracted result needs its remarks and extraction records")
        if self.model_flagged_injection is None:
            raise ValueError("an extracted result needs model_flagged_injection")
        return self


class CachedExtraction(ResultModel):
    """What an extraction call is cached as (`llm_result.result`): the verified remarks signals,
    the dropped claims and the model's flag, never the model's raw answer. `scan_fingerprint`
    names the injection hits the claims were verified against; it is part of the cache key, so a
    result is only ever read back for remarks that scan the same way."""

    version: Literal[1] = 1
    signals: list[StoredSignal]
    dropped: list[DroppedClaim]
    model_flagged_injection: bool
    scan_fingerprint: Sha256Hex

    @model_validator(mode="after")
    def _only_remarks_signals(self) -> Self:
        if any(signal.source != "remarks" for signal in self.signals):
            raise ValueError("a cached extraction holds remarks signals only")
        return self

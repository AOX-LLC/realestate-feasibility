"""RESO Property records for the synthetic listing set, and the one path remarks take into a
listing.

A RESO record carries the listing agent's and office's contact details and private notes. This
module declares only the fields it maps, so those fields are dropped by construction: they are
never read, never validated, never stored. `PublicRemarks` is the one free-text field that
survives, and only through `ingest_remarks`: Unicode normalisation and removal of invisible
characters, then personal-data redaction, then the length cap and the hidden-text marker.
Nothing else sets `Listing.remarks`.

The mock sync reads a committed file of records (`data/mls/<market>.json`) and attaches the
redacted remarks to the RentCast-shaped snapshot listings by their `mlsNumber`. Live mode has
no remarks source: RentCast returns no listing text.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from feasibility.config import Settings
from feasibility.llm.untrusted import finish_untrusted, normalise_untrusted
from feasibility.sources.base import ListingBatch
from feasibility.sources.mls.redact import redact_personal

# Fields a RESO record carries and this adapter never maps. Listed so a test can check that
# the data exercises each one and that none of them reaches a listing; not read by any code.
DROPPED_FIELDS = frozenset(
    {
        "ListAgentFullName",
        "ListAgentDirectPhone",
        "ListAgentEmail",
        "ListOfficeName",
        "ListOfficePhone",
        "PrivateRemarks",
        "ShowingInstructions",
    }
)
# Bound on the work the redactor does for one record. Cut at whitespace, so a number cannot be
# split in two and left half-redacted.
MAX_RAW_REMARKS_CHARS = 20_000
_WHITESPACE_SEARCH = 64


class ResoProperty(BaseModel):
    """The mapped fields of a RESO Property record. Anything else in a record is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    listing_key: str = Field(alias="ListingKey")
    listing_id: str = Field(alias="ListingId")
    unparsed_address: str = Field(alias="UnparsedAddress")
    postal_code: str | None = Field(default=None, alias="PostalCode")
    list_price: Decimal | None = Field(default=None, alias="ListPrice")
    standard_status: str | None = Field(default=None, alias="StandardStatus")
    public_remarks: str | None = Field(default=None, alias="PublicRemarks")


@dataclass(frozen=True)
class IngestedRemarks:
    """Remarks as they may be stored and sent to a model: normalised, redacted and capped."""

    text: str
    redaction_count: int
    removed_invisible_count: int


def _bounded(raw: str) -> str:
    if len(raw) <= MAX_RAW_REMARKS_CHARS:
        return raw
    cut = raw[:MAX_RAW_REMARKS_CHARS]
    last_space = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    return cut[:last_space] if last_space >= MAX_RAW_REMARKS_CHARS - _WHITESPACE_SEARCH else cut


def ingest_remarks(public_remarks: str | None) -> IngestedRemarks | None:
    """The only way remarks enter a listing. None when there is no text left to read."""
    if public_remarks is None:
        return None
    normalised = normalise_untrusted(_bounded(public_remarks))
    redacted = redact_personal(normalised.text)
    text = finish_untrusted(redacted.text, normalised.removed_invisible).strip()
    if not text:
        return None
    return IngestedRemarks(text, redacted.count, normalised.removed_invisible)


def load_reso_records(path: Path) -> list[ResoProperty]:
    """The records of a RESO file: a JSON array of Property objects."""
    return [
        ResoProperty.model_validate(record)
        for record in json.loads(path.read_text(encoding="utf-8"))
    ]


class RemarksSource(Protocol):
    def remarks_for(self, listing_id: str | None) -> IngestedRemarks | None: ...


class NoRemarksSource:
    """Live mode: RentCast listings have no description text."""

    def remarks_for(self, listing_id: str | None) -> IngestedRemarks | None:
        return None


class SnapshotRemarksSource:
    """Remarks from the committed synthetic RESO file, keyed by `ListingId`."""

    def __init__(self, path: Path) -> None:
        self._by_id: dict[str, IngestedRemarks | None] = {}
        for record in load_reso_records(path):
            if record.listing_id in self._by_id:
                raise ValueError(f"{path.name} has two records for {record.listing_id}")
            self._by_id[record.listing_id] = ingest_remarks(record.public_remarks)

    def remarks_for(self, listing_id: str | None) -> IngestedRemarks | None:
        return None if listing_id is None else self._by_id.get(listing_id)


def remarks_source_for(settings: Settings, market: str) -> RemarksSource:
    """Mock mode reads `<mls_dir>/<market>.json` when it exists; live mode has none."""
    if settings.is_live:
        return NoRemarksSource()
    path = settings.mls_dir / f"{market}.json"
    return SnapshotRemarksSource(path) if path.is_file() else NoRemarksSource()


def attach_remarks(batch: ListingBatch, source: RemarksSource) -> ListingBatch:
    """The batch with each listing's remarks set from the source, matched on the feed's own
    `mlsNumber`. A listing the source has nothing for keeps the remarks it came with."""
    listings = []
    for listing in batch.listings:
        found = source.remarks_for(listing.raw.get("mlsNumber"))
        listings.append(
            listing if found is None else listing.model_copy(update={"remarks": found.text})
        )
    return ListingBatch(listings, batch.stale)

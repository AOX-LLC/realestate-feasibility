"""The interfaces source adapters implement. Callers depend on these, never on a
particular provider."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from feasibility.domain import Address, Listing, PropertyRecord, ValueEstimate


class NotConfiguredError(RuntimeError):
    """The source exists as an extension point but has no feed behind it."""


class ListingQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    city: str
    state: str
    status: str = "Active"
    days_old: int = Field(ge=1)
    limit: int = Field(ge=1, le=500)


@dataclass(frozen=True)
class ImportRequest:
    archive: Path
    kind: Literal["certified", "current"]
    # Default: read from the archive name with the pack's archive_pattern.
    roll_year: int | None = None
    # Default: the timestamp of the base member inside the archive.
    file_date: date | None = None
    # Replace an earlier load of the same file key that had different contents.
    force: bool = False


@dataclass
class ImportReport:
    source_file_id: int
    status: Literal["loaded", "unchanged"]
    sha256: str
    file_date: date
    rows_read: int = 0
    rows_loaded: int = 0
    rows_skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)


class ParcelSource(Protocol):
    name: str

    def import_archive(self, request: ImportRequest) -> ImportReport: ...


@dataclass(frozen=True)
class ListingBatch:
    listings: list[Listing]
    # True when the source could not refresh and answered from an expired cache.
    stale: bool = False


class ListingSource(Protocol):
    name: str

    def fetch_listings(self, query: ListingQuery) -> ListingBatch: ...


class PropertyRecordSource(Protocol):
    name: str

    def fetch_property(self, address: Address) -> PropertyRecord | None: ...


class ValuationSource(Protocol):
    name: str

    def estimate_value(self, address: Address) -> ValueEstimate | None: ...

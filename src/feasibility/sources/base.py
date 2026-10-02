"""The interfaces source adapters implement. Callers depend on these, never on a
particular provider."""

from typing import Protocol

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


class ListingSource(Protocol):
    name: str

    def fetch_listings(self, query: ListingQuery) -> list[Listing]: ...


class PropertyRecordSource(Protocol):
    name: str

    def fetch_property(self, address: Address) -> PropertyRecord | None: ...


class ValuationSource(Protocol):
    name: str

    def estimate_value(self, address: Address) -> ValueEstimate | None: ...

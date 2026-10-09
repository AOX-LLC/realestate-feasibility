"""The payload of each job kind. They live apart from the handlers so that a module that only
queues a job (the trigger router) can import them without importing what the handlers run."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from feasibility.markets.schema import FileKind


class CadImportPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str
    # A file name inside the local data directory; jobs never read files elsewhere.
    archive: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    kind: FileKind
    roll_year: int | None = None
    file_date: date | None = None
    force: bool = False


class ListingsSyncPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str


class SourcingRunPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str
    # None means today in the market's time zone (live mode); mock mode needs a date.
    as_of: date | None = None


class MorningRunPayload(BaseModel):
    """The morning chain: source the day, then build its brief."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    market: str
    # None means today in the market's time zone (live mode); mock mode needs a date.
    as_of: date | None = None


class BriefDeliverPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int = Field(ge=1, le=2**63 - 1)

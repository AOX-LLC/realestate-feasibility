"""The market pack: everything county-specific, as validated configuration.

A new county is a new TOML file, not new code. Unknown keys are rejected everywhere so a
typo cannot silently fall back to a default.
"""

import re
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CanonicalField = Literal[
    "gis_parcel_id",
    "street_number",
    "street_half",
    "street_name",
    "unit",
    "city",
    "zip5",
    "land_value",
    "improvement_value",
    "total_value",
    "year_built",
    "living_area_sqft",
    "lot_size_sqft",
    "use_code",
    "zoning",
]
VALUE_FIELDS: frozenset[str] = frozenset({"land_value", "improvement_value", "total_value"})
Transform = Literal["text", "int", "decimal", "zip5"]
FileKind = Literal["certified", "current"]

COLUMN_NAME = r"^[A-Z0-9_]{1,64}$"
# Columns that hold owner identity, contact details, legal descriptions or exemption
# applicants. A pack can never map one, so the importer can never read one.
PERSONAL_DATA_COLUMN = re.compile(r"OWNER|PHONE|BIZ_NAME|LEGAL|TAXPAYER|APPLICANT|MAIL")
AGGREGATE_RULE = re.compile(
    r"^(first|sum|from_max_of:[A-Z0-9_]{1,64}|from_min_of:[A-Z0-9_]{1,64})$"
)
# Values of the skip flag that mean "not set"; any other value skips the account.
DEFAULT_FLAG_NOT_SET = ("", "N", "0", "F", "FALSE")


class PackModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


ColumnName = Annotated[str, Field(pattern=COLUMN_NAME)]


class MarketInfo(PackModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    name: str
    state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    county: str
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value not in available_timezones():
            raise ValueError(f"unknown IANA timezone {value!r}")
        return value


class KindSpec(PackModel):
    # False for files that list every property but carry no values (all zeros);
    # their value columns are never read.
    carries_values: bool


class MemberFile(PackModel):
    member: Annotated[str, Field(pattern=r"^[A-Za-z0-9_. -]{1,128}$")]


class FieldSpec(PackModel):
    file: str
    column: ColumnName
    transform: Transform = "text"
    # How several rows per account collapse to one value. "first" keeps the first
    # row in file order; "from_max_of:COL" keeps the value from the row with the
    # largest COL; "sum" adds them up.
    aggregate: Annotated[str, Field(pattern=AGGREGATE_RULE.pattern)] = "first"
    # Column naming the unit of a measured value; converted with unit_factors.
    unit_column: ColumnName | None = None

    @property
    def order_column(self) -> str | None:
        if ":" not in self.aggregate:
            return None
        return self.aggregate.split(":", 1)[1]


class SkipAccounts(PackModel):
    """Skip a whole account when a flag column is set (e.g. a confidential owner).

    The flag's value only decides the skip; it is never stored.
    """

    file: str
    column: ColumnName
    not_set_values: tuple[str, ...] = DEFAULT_FLAG_NOT_SET


class ParcelSourceSpec(PackModel):
    adapter: Literal["cad_csv"]
    source: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    encoding: str
    delimiter: Annotated[str, Field(min_length=1, max_length=1)]
    # Regex over the archive file name with a named group "year", e.g. DCAD2026_CURRENT.ZIP.
    archive_pattern: str
    join_key: ColumnName
    base_file: str
    kinds: dict[FileKind, KindSpec]
    files: dict[ColumnName, MemberFile]
    fields: dict[CanonicalField, FieldSpec]
    unit_factors: dict[str, Annotated[Decimal, Field(gt=0)]] = Field(default_factory=dict)
    skip_accounts: SkipAccounts | None = None

    @field_validator("archive_pattern")
    @classmethod
    def _pattern_has_year(cls, value: str) -> str:
        try:
            pattern = re.compile(value)
        except re.error as error:
            raise ValueError(f"archive_pattern is not a valid regex: {error}") from None
        if "year" not in pattern.groupindex:
            raise ValueError("archive_pattern needs a named group 'year'")
        return value

    @model_validator(mode="after")
    def _check_references(self) -> "ParcelSourceSpec":
        self._check_files_exist()
        self._check_no_personal_data()
        self._check_base_file_rules()
        return self

    def _check_files_exist(self) -> None:
        referenced = {self.base_file} | {spec.file for spec in self.fields.values()}
        if self.skip_accounts:
            referenced.add(self.skip_accounts.file)
        missing = referenced - set(self.files)
        if missing:
            raise ValueError(f"fields reference undeclared files: {sorted(missing)}")

    def _check_no_personal_data(self) -> None:
        # skip_accounts.column is exempt: its value only decides whether to drop the
        # account and is never staged or stored.
        names = [self.join_key, *self.files, *(m.member for m in self.files.values())]
        for spec in self.fields.values():
            names += [spec.column, spec.unit_column or "", spec.order_column or ""]
        flagged = sorted({name for name in names if PERSONAL_DATA_COLUMN.search(name.upper())})
        if flagged:
            raise ValueError(f"pack maps personal-data files or columns: {flagged}")

    def _check_base_file_rules(self) -> None:
        if self.skip_accounts and self.skip_accounts.file != self.base_file:
            # Skipping works by dropping the base row; other files only join onto it.
            raise ValueError("skip_accounts must use the base file")
        for name, spec in self.fields.items():
            if spec.file == self.base_file and spec.aggregate != "first":
                raise ValueError(f"{name}: the base file has one row per account; use 'first'")
            if spec.aggregate == "sum" and spec.transform not in ("int", "decimal"):
                raise ValueError(f"{name}: 'sum' needs transform 'int' or 'decimal'")
            if spec.unit_column and spec.transform != "decimal":
                raise ValueError(f"{name}: unit conversion needs transform 'decimal'")
            if spec.unit_column and not self.unit_factors:
                raise ValueError(f"{name}: unit_column set but no unit_factors declared")

    def fields_for(self, kind: FileKind) -> dict[str, FieldSpec]:
        """The fields to read for a file kind: value columns only when it carries values."""
        include_values = self.kinds[kind].carries_values
        return {
            name: spec
            for name, spec in self.fields.items()
            if include_values or name not in VALUE_FIELDS
        }


class RentCastListings(PackModel):
    adapter: Literal["rentcast"]
    enabled: bool = True
    city: str
    state: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    status: Literal["Active", "Inactive"] = "Active"
    days_old: Annotated[int, Field(ge=1)]
    limit: Annotated[int, Field(ge=1, le=500)]


class MlsListings(PackModel):
    adapter: Literal["mls"]
    enabled: bool = False


ListingSourceSpec = Annotated[RentCastListings | MlsListings, Field(discriminator="adapter")]


class Sources(PackModel):
    parcels: ParcelSourceSpec
    listings: list[ListingSourceSpec]


class BuyBox(PackModel):
    """Which candidates the builder wants. Applied by sourcing/filters.py."""

    zips: list[Annotated[str, Field(pattern=r"^\d{5}$")]] = Field(min_length=1)
    price_max: Annotated[Decimal, Field(gt=0)]
    lot_size_min_sqft: Annotated[Decimal, Field(ge=0)]
    year_built_max: Annotated[int, Field(ge=1800, le=2100)]
    land_to_total_min: Annotated[Decimal, Field(ge=0, le=1)]
    property_types: list[str] = Field(min_length=1)


Weight = Annotated[Decimal, Field(ge=0, le=100)]


class Scoring(PackModel):
    """Teardown-score weights and the points where each component earns full credit.

    The zero points come from the buy box: the land-ratio floor, the age cut-off year and
    the lot-size floor.
    """

    land_ratio_weight: Weight
    land_ratio_full: Annotated[Decimal, Field(gt=0, le=1)]
    age_weight: Weight
    age_full_year: Annotated[int, Field(ge=1800, le=2100)]
    lot_weight: Weight
    lot_full_sqft: Annotated[Decimal, Field(gt=0)]
    price_land_weight: Weight
    # Price divided by land value: full credit at or below _full, nothing at or above _zero.
    price_land_full: Annotated[Decimal, Field(gt=0)]
    price_land_zero: Annotated[Decimal, Field(gt=0)]
    # Fraction of the age points a vacant lot earns.
    vacant_age_credit: Annotated[Decimal, Field(ge=0, le=1)]
    # Appraisal values are as of January 1 of the roll year; land value is drifted forward.
    value_drift_pct_per_year: Annotated[Decimal, Field(ge=0, le=30)]
    max_drift_years: Annotated[Decimal, Field(gt=0, le=5)]
    stale_values_years: Annotated[Decimal, Field(gt=0, le=5)]


class Sourcing(PackModel):
    source_priority: list[str] = Field(min_length=1)  # highest first; names match listing.source
    scoring: Scoring


class CostAssumptions(PackModel):
    """Inputs to the pro-forma. Placeholders until the pro-forma engine sets real ones."""

    status: Literal["placeholder", "reviewed"]
    hard_cost_per_sqft: Decimal | None = None
    soft_cost_pct: Decimal | None = None
    financing_rate_pct: Decimal | None = None
    points_pct: Decimal | None = None
    hold_months: Decimal | None = None
    selling_cost_pct: Decimal | None = None


class MarketPack(PackModel):
    market: MarketInfo
    sources: Sources
    buy_box: BuyBox
    sourcing: Sourcing
    cost_assumptions: CostAssumptions

    @model_validator(mode="after")
    def _check_sourcing_against_buy_box(self) -> "MarketPack":
        scoring, box = self.sourcing.scoring, self.buy_box
        weights = (
            scoring.land_ratio_weight
            + scoring.age_weight
            + scoring.lot_weight
            + scoring.price_land_weight
        )
        if weights != 100:
            raise ValueError(f"scoring weights must sum to 100, got {weights}")
        if scoring.age_full_year >= box.year_built_max:
            raise ValueError("age_full_year must be earlier than buy_box.year_built_max")
        if scoring.land_ratio_full <= box.land_to_total_min:
            raise ValueError("land_ratio_full must exceed buy_box.land_to_total_min")
        if scoring.lot_full_sqft <= box.lot_size_min_sqft:
            raise ValueError("lot_full_sqft must exceed buy_box.lot_size_min_sqft")
        if scoring.price_land_full >= scoring.price_land_zero:
            raise ValueError("price_land_full must be below price_land_zero")
        priority = self.sourcing.source_priority
        if len(set(priority)) != len(priority):
            raise ValueError("source_priority must not repeat a source")
        unknown = set(priority) - {spec.adapter for spec in self.sources.listings}
        if unknown:
            raise ValueError(f"source_priority names unconfigured sources: {sorted(unknown)}")
        return self

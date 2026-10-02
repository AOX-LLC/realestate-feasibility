"""Streams one CSV member of a county archive, projecting only the columns a pack maps.

Memory stays at one row: the member is decompressed and decoded as it is read, and
columns the pack does not name are never even split out into values that are kept.
"""

import csv
import io
import zipfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from feasibility.domain import zip5
from feasibility.markets.schema import FieldSpec, SkipAccounts

StagedType = Literal["text", "int", "decimal", "bool"]
STAGING_SQL_TYPES: dict[StagedType, str] = {
    "text": "text",
    "int": "bigint",
    "decimal": "numeric",
    "bool": "boolean",
}
StagedValue = str | int | Decimal | bool | None
SQL_TYPE_FOR_TRANSFORM: dict[str, StagedType] = {
    "text": "text",
    "zip5": "text",
    "int": "int",
    "decimal": "decimal",
}


class MemberFormatError(ValueError):
    """The member is missing columns the pack maps."""


@dataclass(frozen=True)
class StagedColumn:
    """One staging-table column and how to fill it from a CSV row."""

    name: str
    source_column: str
    sql_type: StagedType
    transform: str = "text"
    unit_column: str | None = None
    unit_flag_for: str | None = None


@dataclass(frozen=True)
class MemberPlan:
    file_key: str
    member: str
    join_key: str
    columns: tuple[StagedColumn, ...]
    skip_accounts: SkipAccounts | None = None


def plan_member(
    file_key: str,
    member: str,
    join_key: str,
    fields: dict[str, FieldSpec],
    skip_accounts: SkipAccounts | None,
) -> MemberPlan:
    """The staging columns for one file: each mapped field, an 'unknown unit' flag for
    unit-converted fields and the numeric column each from_max/from_min rule orders by."""
    columns: list[StagedColumn] = []
    order_columns: set[str] = set()
    for name, spec in fields.items():
        if spec.file != file_key:
            continue
        sql_type = SQL_TYPE_FOR_TRANSFORM[spec.transform]
        columns.append(StagedColumn(name, spec.column, sql_type, spec.transform, spec.unit_column))
        if spec.unit_column:
            columns.append(
                StagedColumn(f"{name}__unit_unknown", spec.unit_column, "bool", unit_flag_for=name)
            )
        if spec.order_column:
            order_columns.add(spec.order_column)
    for column in sorted(order_columns):
        columns.append(StagedColumn(order_column_name(column), column, "decimal", "decimal"))
    return MemberPlan(file_key, member, join_key, tuple(columns), skip_accounts)


def order_column_name(source_column: str) -> str:
    return f"order__{source_column.lower()}"


@dataclass
class ReadStats:
    rows_read: int = 0
    rows_skipped: int = 0
    reasons: Counter[str] | None = None

    def note(self, reason: str, *, skipped: bool = False) -> None:
        if self.reasons is None:
            self.reasons = Counter()
        self.reasons[reason] += 1
        if skipped:
            self.rows_skipped += 1


def iter_member_rows(
    archive: zipfile.ZipFile,
    plan: MemberPlan,
    *,
    encoding: str,
    delimiter: str,
    unit_factors: dict[str, Decimal],
    stats: ReadStats,
) -> Iterator[tuple[StagedValue, ...]]:
    """Yield (account_id, row_no, *staged values) for each kept row."""
    with archive.open(plan.member) as raw:
        text = io.TextIOWrapper(raw, encoding=encoding, newline="")
        reader = csv.reader(text, delimiter=delimiter)
        header = [name.strip() for name in next(reader, [])]
        positions = _column_positions(plan, header)
        width = max(positions.values()) + 1

        for row_no, row in enumerate(reader, start=1):
            stats.rows_read += 1
            if len(row) < width:
                stats.note(f"{plan.file_key}:short_row", skipped=True)
                continue
            account_id = row[positions[plan.join_key]].strip()
            if not account_id:
                stats.note(f"{plan.file_key}:missing_join_key", skipped=True)
                continue
            if _flag_is_set(row, positions, plan.skip_accounts):
                stats.note(f"{plan.file_key}:skip_flag", skipped=True)
                continue
            values = _staged_values(row, positions, plan, unit_factors, stats)
            yield (account_id, row_no, *values)


def _column_positions(plan: MemberPlan, header: Sequence[str]) -> dict[str, int]:
    wanted = {plan.join_key}
    for column in plan.columns:
        wanted.add(column.source_column)
        if column.unit_column:
            wanted.add(column.unit_column)
    if plan.skip_accounts:
        wanted.add(plan.skip_accounts.column)
    missing = sorted(wanted - set(header))
    if missing:
        raise MemberFormatError(f"{plan.member} is missing columns {missing}")
    return {name: header.index(name) for name in wanted}


def _flag_is_set(row: list[str], positions: dict[str, int], rule: SkipAccounts | None) -> bool:
    if rule is None:
        return False
    return row[positions[rule.column]].strip().upper() not in rule.not_set_values


def _staged_values(
    row: list[str],
    positions: dict[str, int],
    plan: MemberPlan,
    unit_factors: dict[str, Decimal],
    stats: ReadStats,
) -> list[StagedValue]:
    values: list[StagedValue] = []
    unknown_units: set[str] = set()
    for column in plan.columns:
        if column.unit_flag_for:
            # The flag column always follows its field, so the field is already converted.
            values.append(column.unit_flag_for in unknown_units)
            continue
        raw = row[positions[column.source_column]].strip()
        try:
            value = _transform(raw, column.transform)
        except ValueError:
            stats.note(f"{plan.file_key}:{column.name}:invalid")
            value = None
        if column.unit_column and isinstance(value, Decimal):
            unit = row[positions[column.unit_column]].strip().upper()
            factor = unit_factors.get(unit)
            if factor is None:
                stats.note(f"{plan.file_key}:{column.name}:unknown_unit:{unit or 'blank'}")
                unknown_units.add(column.name)
                value = None
            else:
                value = value * factor
        values.append(value)
    return values


def _transform(raw: str, transform: str) -> StagedValue:
    """Parse one stripped cell. Raises ValueError for a value the transform cannot read."""
    if not raw:
        return None
    if transform == "zip5":
        return zip5(raw)
    if transform == "text":
        return raw
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"not a number: {raw!r}") from None
    if transform == "decimal":
        return number
    if number != number.to_integral_value():
        raise ValueError(f"not a whole number: {raw!r}")
    return int(number)

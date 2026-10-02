"""Imports a county appraisal archive into parcel_version and parcel.

The pack drives everything; this module knows no county. Steps:
1. hash the archive (streaming) and register it in source_file, or stop if it is
   already loaded with the same contents;
2. stream each member file through COPY into a temporary staging table;
3. collapse each file to one row per account and join them in one INSERT ... SELECT;
4. upsert parcel: attributes from any file at least as new as the last one, values
   only from files that carry values.
Steps 2-4 run in one transaction, so a failure leaves nothing half-loaded.

SQL identifiers built here come only from validated pack keys (upper-case column names,
fixed canonical field names), never from file contents.
"""

import hashlib
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import psycopg
from sqlalchemy import Connection, Engine, text

from feasibility.markets.schema import (
    VALUE_FIELDS,
    FieldSpec,
    FileKind,
    MarketPack,
    ParcelSourceSpec,
)
from feasibility.sources.base import ImportReport, ImportRequest
from feasibility.sources.cad_csv.reader import (
    STAGING_SQL_TYPES,
    MemberPlan,
    ReadStats,
    iter_member_rows,
    order_column_name,
    plan_member,
)

log = logging.getLogger(__name__)

HASH_CHUNK_BYTES = 1 << 20
ERROR_TEXT_LIMIT = 2000
PARCEL_ATTRIBUTE_FIELDS = (
    "gis_parcel_id",
    "street_number",
    "street_half",
    "street_name",
    "unit",
    "city",
    "zip5",
    "year_built",
    "living_area_sqft",
    "lot_size_sqft",
    "use_code",
    "zoning",
)
PARCEL_VALUE_FIELDS = tuple(sorted(VALUE_FIELDS))


class CadImportError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        while chunk := archive.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class CadCsvParcelSource:
    """ParcelSource for any county whose bulk export is a zip of delimited files."""

    def __init__(self, engine: Engine, pack: MarketPack) -> None:
        self._engine = engine
        self._market = pack.market.id
        self._spec = pack.sources.parcels
        self.name = self._spec.source

    def import_archive(self, request: ImportRequest) -> ImportReport:
        if request.kind not in self._spec.kinds:
            raise CadImportError(f"the pack has no file kind {request.kind!r}")
        sha256 = sha256_of(request.archive)
        with zipfile.ZipFile(request.archive) as archive:
            members = self._member_infos(archive)
            file_date = request.file_date or _member_date(members[self._spec.base_file])
            roll_year = request.roll_year or self._roll_year_from_name(request.archive)
            key = _SourceFileKey(self._market, self.name, request.kind, roll_year, file_date)

            existing = self._register(key, sha256, force=request.force)
            if isinstance(existing, ImportReport):
                return existing
            try:
                report = self._load(archive, key, existing, sha256)
            except Exception as error:
                self._mark_failed(existing, error)
                raise
        log.info(
            "imported %s %s %s: %d read, %d loaded, %d skipped",
            self.name,
            request.kind,
            file_date,
            report.rows_read,
            report.rows_loaded,
            report.rows_skipped,
        )
        return report

    def _member_infos(self, archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        names = set(archive.namelist())
        missing = sorted(f.member for f in self._spec.files.values() if f.member not in names)
        if missing:
            raise CadImportError(f"archive is missing members {missing}")
        return {key: archive.getinfo(f.member) for key, f in self._spec.files.items()}

    def _roll_year_from_name(self, archive: Path) -> int:
        match = re.match(self._spec.archive_pattern, archive.name)
        if match is None:
            raise CadImportError(
                f"cannot read the roll year from {archive.name!r}; pass it explicitly"
            )
        return int(match.group("year"))

    def _register(self, key: "_SourceFileKey", sha256: str, *, force: bool) -> int | ImportReport:
        """Claim the source_file row for this load. Returns its id, or the report of the
        identical load that already happened."""
        carries_values = self._spec.kinds[key.kind].carries_values
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, sha256, status, rows_read, rows_loaded, rows_skipped,
                           skip_reasons
                    FROM source_file
                    WHERE market = :market AND source = :source AND kind = :kind
                      AND roll_year = :roll_year AND file_date = :file_date
                    FOR UPDATE
                    """
                ),
                key.params(),
            ).first()
            if row is None:
                return int(
                    connection.execute(
                        text(
                            """
                            INSERT INTO source_file (market, source, kind, roll_year,
                                file_date, sha256, carries_values, status)
                            VALUES (:market, :source, :kind, :roll_year, :file_date,
                                :sha256, :carries_values, 'loading')
                            RETURNING id
                            """
                        ),
                        {**key.params(), "sha256": sha256, "carries_values": carries_values},
                    ).scalar_one()
                )
            if row.status == "loaded" and row.sha256 == sha256:
                return ImportReport(
                    source_file_id=row.id,
                    status="unchanged",
                    sha256=sha256,
                    file_date=key.file_date,
                    rows_read=row.rows_read,
                    rows_loaded=row.rows_loaded,
                    rows_skipped=row.rows_skipped,
                    skip_reasons=row.skip_reasons,
                )
            if row.status == "loaded" and not force:
                raise CadImportError(
                    f"{key} was already loaded from different contents; pass force to replace it"
                )
            connection.execute(
                text(
                    """
                    UPDATE source_file
                    SET sha256 = :sha256, status = 'loading', error = NULL, completed_at = NULL
                    WHERE id = :id
                    """
                ),
                {"id": row.id, "sha256": sha256},
            )
            return int(row.id)

    def _load(
        self, archive: zipfile.ZipFile, key: "_SourceFileKey", source_file_id: int, sha256: str
    ) -> ImportReport:
        fields = self._spec.fields_for(key.kind)
        plans = self._member_plans(fields)
        stats = ReadStats()
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM parcel_version WHERE source_file_id = :id"),
                {"id": source_file_id},
            )
            for plan in plans:
                self._stage(connection, archive, plan, stats)
            rows_loaded = _insert_versions(
                connection, self._spec, fields, plans, self._market, source_file_id
            )
            _upsert_parcels(
                connection, source_file_id, key.file_date, self._spec.kinds[key.kind].carries_values
            )
            removed = _remove_flagged_accounts(connection, self._market, stats.flagged_accounts)
            reasons = dict(stats.reasons or {})
            if removed:
                reasons["removed_flagged_accounts"] = removed
            connection.execute(
                text(
                    """
                    UPDATE source_file
                    SET status = 'loaded', rows_read = :read, rows_loaded = :loaded,
                        rows_skipped = :skipped, skip_reasons = CAST(:reasons AS jsonb),
                        completed_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": source_file_id,
                    "read": stats.rows_read,
                    "loaded": rows_loaded,
                    "skipped": stats.rows_skipped,
                    "reasons": json.dumps(reasons),
                },
            )
        return ImportReport(
            source_file_id=source_file_id,
            status="loaded",
            sha256=sha256,
            file_date=key.file_date,
            rows_read=stats.rows_read,
            rows_loaded=rows_loaded,
            rows_skipped=stats.rows_skipped,
            skip_reasons=reasons,
        )

    def _member_plans(self, fields: dict[str, FieldSpec]) -> list[MemberPlan]:
        """One plan per file that has fields to read; the base file always comes first."""
        used_files = {spec.file for spec in fields.values()} | {self._spec.base_file}
        ordered = [self._spec.base_file, *sorted(used_files - {self._spec.base_file})]
        return [
            plan_member(
                file_key,
                self._spec.files[file_key].member,
                self._spec.join_key,
                fields,
                self._spec.skip_accounts if file_key == self._spec.base_file else None,
            )
            for file_key in ordered
        ]

    def _stage(
        self, connection: Connection, archive: zipfile.ZipFile, plan: MemberPlan, stats: ReadStats
    ) -> None:
        table = _staging_table(plan.file_key)
        column_defs = ", ".join(
            f"{column.name} {STAGING_SQL_TYPES[column.sql_type]}" for column in plan.columns
        )
        # Temporary tables are never WAL-logged and vanish at commit.
        connection.execute(
            text(
                f"CREATE TEMPORARY TABLE {table} (account_id text NOT NULL, row_no bigint NOT NULL"
                f"{', ' + column_defs if column_defs else ''}) ON COMMIT DROP"
            )
        )
        column_names = ", ".join(["account_id", "row_no", *(c.name for c in plan.columns)])
        rows = iter_member_rows(
            archive,
            plan,
            encoding=self._spec.encoding,
            delimiter=self._spec.delimiter,
            unit_factors=dict(self._spec.unit_factors),
            stats=stats,
        )
        driver = cast(psycopg.Connection[Any], connection.connection.driver_connection)
        with (
            driver.cursor() as cursor,
            cursor.copy(f"COPY {table} ({column_names}) FROM STDIN") as copy,
        ):
            for row in rows:
                copy.write_row(row)

    def _mark_failed(self, source_file_id: int, error: Exception) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE source_file SET status = 'failed', error = :error, "
                    "completed_at = now() WHERE id = :id"
                ),
                {
                    "id": source_file_id,
                    "error": f"{type(error).__name__}: {error}"[:ERROR_TEXT_LIMIT],
                },
            )


@dataclass(frozen=True)
class _SourceFileKey:
    market: str
    source: str
    kind: FileKind
    roll_year: int
    file_date: date

    def params(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "source": self.source,
            "kind": self.kind,
            "roll_year": self.roll_year,
            "file_date": self.file_date,
        }

    def __str__(self) -> str:
        return f"{self.market}/{self.source} {self.kind} {self.roll_year} {self.file_date}"


def _member_date(info: zipfile.ZipInfo) -> date:
    year, month, day, *_ = info.date_time
    return date(year, month, day)


SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _identifier(name: str) -> str:
    """Defence in depth: the pack schema already restricts these names."""
    if not SQL_IDENTIFIER.match(name):
        raise CadImportError(f"refusing to use {name!r} as an SQL identifier")
    return name


def _staging_table(file_key: str) -> str:
    return _identifier(f"stage_{file_key.lower()}")


def _aggregate_table(file_key: str) -> str:
    return _identifier(f"agg_{file_key.lower()}")


def _aggregate_expression(name: str, spec: FieldSpec, *, has_unit_flag: bool) -> str:
    """SQL collapsing one field's rows for an account to a single value."""
    if spec.aggregate == "sum":
        total = f"sum({name})"
        if has_unit_flag:
            # One section in an unknown unit makes the account's total unknowable.
            return f"CASE WHEN bool_or({name}__unit_unknown) THEN NULL ELSE {total} END"
        return total
    if spec.aggregate.startswith("from_max_of:"):
        order = f"{order_column_name(cast(str, spec.order_column))} DESC NULLS LAST, row_no"
    elif spec.aggregate.startswith("from_min_of:"):
        order = f"{order_column_name(cast(str, spec.order_column))} ASC NULLS LAST, row_no"
    else:
        order = "row_no"
    return f"(array_agg({name} ORDER BY {order}))[1]"


def _insert_versions(
    connection: Connection,
    spec: ParcelSourceSpec,
    fields: dict[str, FieldSpec],
    plans: list[MemberPlan],
    market: str,
    source_file_id: int,
) -> int:
    aggregated: list[str] = []
    for plan in plans:
        unit_flagged = {c.unit_flag_for for c in plan.columns if c.unit_flag_for}
        expressions = [
            f"{_aggregate_expression(name, field_spec, has_unit_flag=name in unit_flagged)} "
            f"AS {name}"
            for name, field_spec in fields.items()
            if field_spec.file == plan.file_key
        ]
        select_list = ", ".join(["account_id", *expressions])
        aggregated.append(
            f"{_aggregate_table(plan.file_key)} AS (SELECT {select_list} "  # noqa: S608
            f"FROM {_staging_table(plan.file_key)} GROUP BY account_id)"
        )

    base = _aggregate_table(spec.base_file)
    joins = " ".join(
        f"LEFT JOIN {_aggregate_table(plan.file_key)} USING (account_id)"
        for plan in plans
        if plan.file_key != spec.base_file
    )
    columns = [_identifier(name) for name in fields]
    statement = (
        f"WITH {', '.join(aggregated)} "  # noqa: S608
        f"INSERT INTO parcel_version (market, account_id, source_file_id"
        f"{''.join(', ' + c for c in columns)}) "
        f"SELECT :market, account_id, :source_file_id{''.join(', ' + c for c in columns)} "
        f"FROM {base} {joins}"
    )
    result = connection.execute(
        text(statement), {"market": market, "source_file_id": source_file_id}
    )
    return result.rowcount


def _remove_flagged_accounts(connection: Connection, market: str, accounts: list[str]) -> int:
    """Delete every stored row of accounts the skip flag now marks, so an account flagged
    after an earlier load does not linger. Returns how many parcels were removed."""
    if not accounts:
        return 0
    connection.execute(
        text("CREATE TEMPORARY TABLE flagged_account (account_id text) ON COMMIT DROP")
    )
    driver = cast(psycopg.Connection[Any], connection.connection.driver_connection)
    with driver.cursor() as cursor, cursor.copy("COPY flagged_account FROM STDIN") as copy:
        for account_id in accounts:
            copy.write_row((account_id,))
    params = {"market": market}
    connection.execute(
        text(
            "DELETE FROM parcel_version WHERE market = :market"
            " AND account_id IN (SELECT account_id FROM flagged_account)"
        ),
        params,
    )
    removed = connection.execute(
        text(
            "DELETE FROM parcel WHERE market = :market"
            " AND account_id IN (SELECT account_id FROM flagged_account)"
        ),
        params,
    )
    return removed.rowcount


def _upsert_parcels(
    connection: Connection, source_file_id: int, file_date: date, carries_values: bool
) -> None:
    attributes = ", ".join(PARCEL_ATTRIBUTE_FIELDS)
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in PARCEL_ATTRIBUTE_FIELDS)
    # Attributes follow the newest file of any kind; ties go to the latest import.
    connection.execute(
        text(
            f"""
            INSERT INTO parcel (market, account_id, {attributes}, attrs_file_date)
            SELECT market, account_id, {attributes}, :file_date
            FROM parcel_version WHERE source_file_id = :source_file_id
            ON CONFLICT (market, account_id) DO UPDATE
            SET {updates}, attrs_file_date = EXCLUDED.attrs_file_date, updated_at = now()
            WHERE parcel.attrs_file_date <= EXCLUDED.attrs_file_date
            """  # noqa: S608
        ),
        {"source_file_id": source_file_id, "file_date": file_date},
    )
    if not carries_values:
        # A values-free file must never blank the certified values.
        return
    value_updates = ", ".join(f"{name} = v.{name}" for name in PARCEL_VALUE_FIELDS)
    connection.execute(
        text(
            f"""
            UPDATE parcel AS p
            SET {value_updates}, values_file_date = :file_date, updated_at = now()
            FROM parcel_version AS v
            WHERE v.source_file_id = :source_file_id
              AND p.market = v.market AND p.account_id = v.account_id
              AND (p.values_file_date IS NULL OR p.values_file_date <= :file_date)
            """  # noqa: S608
        ),
        {"source_file_id": source_file_id, "file_date": file_date},
    )

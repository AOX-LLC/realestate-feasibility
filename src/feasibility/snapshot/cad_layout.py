"""Writes synthetic CSVs in the layout of a Dallas Central Appraisal District bulk file.

The real files quote every field (numbers too), pad values with spaces, end lines with
CRLF and write money as '.00'. The synthetic fixtures copy all of that so the importer
is tested against the real format. Column order here is illustrative; the importer
locates columns by header name.

The personal-data columns are present on purpose, filled with an obvious marker, so tests
can prove the importer never reads them.
"""

import csv
import io
import zipfile
from collections.abc import Iterable, Mapping
from datetime import datetime

DO_NOT_IMPORT = "SYNTHETIC-DO-NOT-IMPORT"

ACCOUNT_INFO_HEADER = [
    "ACCOUNT_NUM", "APPRAISAL_YR", "DIVISION_CD", "BIZ_NAME", "OWNER_NAME1", "OWNER_NAME2",
    "EXCLUDE_OWNER", "OWNER_ADDRESS_LINE1", "OWNER_ADDRESS_LINE2", "OWNER_ADDRESS_LINE3",
    "OWNER_ADDRESS_LINE4", "OWNER_CITY", "OWNER_STATE", "OWNER_ZIPCODE", "OWNER_COUNTRY",
    "STREET_NUM", "STREET_HALF_NUM", "FULL_STREET_NAME", "BLDG_ID", "UNIT_ID",
    "PROPERTY_CITY", "PROPERTY_ZIPCODE", "MAPSCO", "NBHD_CD", "LEGAL1", "LEGAL2", "LEGAL3",
    "LEGAL4", "LEGAL5", "DEED_TXFR_DATE", "GIS_PARCEL_ID", "PHONE_NUM",
]  # fmt: skip
ACCOUNT_APPRL_YEAR_HEADER = [
    "ACCOUNT_NUM", "APPRAISAL_YR", "IMPR_VAL", "LAND_VAL", "LAND_AG_EXEMPT", "AG_USE_VAL",
    "TOT_VAL", "HMSTD_VAL", "PREV_MKT_VAL", "TOT_CONTRIB_AMT", "TAXPAYER_REP",
    "CITY_JURIS_DESC", "ISD_JURIS_DESC", "SPTD_CODE", "DIVISION_CD",
]  # fmt: skip
RES_DETAIL_HEADER = [
    "ACCOUNT_NUM", "APPRAISAL_YR", "TAX_OBJ_ID", "BLDG_CLASS_DESC", "YR_BUILT",
    "EFF_YR_BUILT", "ACT_AGE", "CDU_RATING_DESC", "TOT_MAIN_SF", "TOT_LIVING_AREA_SF",
    "PCT_COMPLETE", "NUM_STORIES_DESC",
]  # fmt: skip
LAND_HEADER = [
    "ACCOUNT_NUM", "APPRAISAL_YR", "SECTION_NUM", "SPTD_CD", "SPTD_DESC", "ZONING",
    "FRONT_DIM", "DEPTH_DIM", "AREA_SIZE", "AREA_UOM_DESC", "COST_PER_UOM", "VAL_AMT",
]  # fmt: skip

HEADERS = {
    "ACCOUNT_INFO": ACCOUNT_INFO_HEADER,
    "ACCOUNT_APPRL_YEAR": ACCOUNT_APPRL_YEAR_HEADER,
    "RES_DETAIL": RES_DETAIL_HEADER,
    "LAND": LAND_HEADER,
}
TEXT_PAD_WIDTH = 24


def money(value: int | None) -> str:
    """DCAD writes money with two decimals and no leading zero for zero: '.00'."""
    if not value:
        return ".00"
    return f"{value}.00"


def format_csv(header: list[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    """Every field quoted, text padded with trailing spaces, CRLF line endings.
    Columns a row leaves out get the do-not-import marker (personal-data columns) or blank."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(column, row) for column in header])
    return buffer.getvalue().encode("ascii")


def _cell(column: str, row: Mapping[str, str]) -> str:
    if column in row:
        value = row[column]
        return value.ljust(TEXT_PAD_WIDTH) if value and not value[0].isdigit() else value
    if any(marker in column for marker in ("OWNER_", "BIZ_NAME", "LEGAL", "TAXPAYER", "PHONE")):
        return DO_NOT_IMPORT
    return ""


def write_archive(
    path: str | io.BytesIO,
    files: Mapping[str, Iterable[Mapping[str, str]]],
    member_time: datetime,
) -> None:
    """Write an archive shaped like DCAD{YYYY}_CURRENT.ZIP, with a fixed member timestamp
    so the same rows always produce byte-identical archives."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_key, rows in files.items():
            info = zipfile.ZipInfo(f"{file_key}.CSV", date_time=member_time.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, format_csv(HEADERS[file_key], rows))

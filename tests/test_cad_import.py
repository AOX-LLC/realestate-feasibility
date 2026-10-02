import tracemalloc
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from feasibility.jobs.handlers import resolve_local_file
from feasibility.markets.loader import get_pack
from feasibility.snapshot.cad_layout import DO_NOT_IMPORT, money, write_archive
from feasibility.sources.base import ImportRequest
from feasibility.sources.cad_csv.importer import CadCsvParcelSource, CadImportError

MEMBER_TIME = datetime(2026, 9, 15, 6, 0, 0)
ACCOUNT_A = "99000000000000001"
ACCOUNT_B = "99000000000000002"
SECRET_OWNER = "99000000000000003"


def _account(account: str, street: str, *, exclude: str = "") -> dict[str, str]:
    return {
        "ACCOUNT_NUM": account,
        "APPRAISAL_YR": "2026",
        "STREET_NUM": "100",
        "FULL_STREET_NAME": street,
        "PROPERTY_CITY": "DALLAS",
        "PROPERTY_ZIPCODE": "752141234",
        "GIS_PARCEL_ID": f"G{account[-4:]}",
        "EXCLUDE_OWNER": exclude,
    }


def _values(account: str, land: int, improvements: int) -> dict[str, str]:
    return {
        "ACCOUNT_NUM": account,
        "APPRAISAL_YR": "2026",
        "LAND_VAL": money(land),
        "IMPR_VAL": money(improvements),
        "TOT_VAL": money(land + improvements),
        "SPTD_CODE": "A11",
    }


def _files(*, values: bool = True, street_a: str = "SYNTHETIC ELM ST") -> dict[str, Any]:
    zero = 0
    return {
        "ACCOUNT_INFO": [
            _account(ACCOUNT_A, street_a),
            _account(ACCOUNT_B, "SYNTHETIC OAK AVE"),
            _account(SECRET_OWNER, "SYNTHETIC PINE DR", exclude="Y"),
        ],
        "ACCOUNT_APPRL_YEAR": [
            _values(ACCOUNT_A, 300000 if values else zero, 90000 if values else zero),
            _values(ACCOUNT_B, 200000 if values else zero, 250000 if values else zero),
            _values(SECRET_OWNER, 1, 1),
        ],
        "RES_DETAIL": [
            {"ACCOUNT_NUM": ACCOUNT_A, "YR_BUILT": "1948", "TOT_LIVING_AREA_SF": "1400"},
            {"ACCOUNT_NUM": ACCOUNT_A, "YR_BUILT": "1990", "TOT_LIVING_AREA_SF": "400"},
            {"ACCOUNT_NUM": ACCOUNT_B, "YR_BUILT": "1962", "TOT_LIVING_AREA_SF": "2100"},
        ],
        "LAND": [
            {"ACCOUNT_NUM": ACCOUNT_A, "SECTION_NUM": "2", "ZONING": "R-5(A)",
             "AREA_SIZE": "1000", "AREA_UOM_DESC": "SQUARE FEET"},
            {"ACCOUNT_NUM": ACCOUNT_A, "SECTION_NUM": "1", "ZONING": "R-7.5(A)",
             "AREA_SIZE": ".25", "AREA_UOM_DESC": "ACRES"},
            {"ACCOUNT_NUM": ACCOUNT_B, "SECTION_NUM": "1", "ZONING": "CD-12",
             "AREA_SIZE": "5", "AREA_UOM_DESC": "FRONT FEET"},
        ],
    }  # fmt: skip


def _archive(tmp_path: Path, name: str = "DCAD2026_CURRENT.ZIP", **options: Any) -> Path:
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / name
    write_archive(str(path), _files(**options), MEMBER_TIME)
    return path


def _source(engine: Engine) -> CadCsvParcelSource:
    return CadCsvParcelSource(engine, get_pack("dallas"))


def _parcel(engine: Engine, account: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT * FROM parcel WHERE market = 'dallas' AND account_id = :a"),
            {"a": account},
        ).first()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def test_certified_import_aggregates_each_file_to_one_parcel(
    engine: Engine, tmp_path: Path
) -> None:
    report = _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))

    assert report.status == "loaded"
    assert report.file_date == date(2026, 9, 15)
    assert report.rows_loaded == 2
    parcel = _parcel(engine, ACCOUNT_A)
    assert parcel.street_name == "SYNTHETIC ELM ST"
    assert parcel.zip5 == "75214"
    assert (parcel.land_value, parcel.total_value) == (Decimal("300000.00"), Decimal("390000.00"))
    assert parcel.living_area_sqft == 1800
    assert parcel.year_built == 1948
    assert parcel.lot_size_sqft == Decimal("1000") + Decimal("0.25") * 43560
    assert parcel.zoning == "R-7.5(A)"
    assert parcel.values_file_date == date(2026, 9, 15)


def test_unknown_unit_nulls_the_lot_size_and_is_counted(engine: Engine, tmp_path: Path) -> None:
    report = _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))

    assert _parcel(engine, ACCOUNT_B).lot_size_sqft is None
    assert report.skip_reasons["LAND:lot_size_sqft:unknown_unit:FRONT FEET"] == 1


def test_flagged_account_is_skipped_whole_and_counted(engine: Engine, tmp_path: Path) -> None:
    report = _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))

    assert _parcel(engine, SECRET_OWNER) is None
    assert report.skip_reasons["ACCOUNT_INFO:skip_flag"] == 1
    assert report.rows_skipped == 1


def test_personal_data_columns_never_reach_the_database(engine: Engine, tmp_path: Path) -> None:
    _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))

    with engine.connect() as connection:
        dumped = connection.execute(
            text(
                "SELECT string_agg(t::text, ' ') FROM ("
                " SELECT row_to_json(p)::text AS t FROM parcel p"
                " UNION ALL SELECT row_to_json(v)::text FROM parcel_version v"
                " UNION ALL SELECT row_to_json(s)::text FROM source_file s) x"
            )
        ).scalar_one()
    assert DO_NOT_IMPORT not in dumped


def test_reimporting_the_same_archive_is_a_no_op(engine: Engine, tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    first = _source(engine).import_archive(ImportRequest(archive, "certified"))

    second = _source(engine).import_archive(ImportRequest(archive, "certified"))

    assert second.status == "unchanged"
    assert second.source_file_id == first.source_file_id
    assert (_count(engine, "parcel"), _count(engine, "parcel_version")) == (2, 2)


def test_changed_contents_under_the_same_key_need_force(engine: Engine, tmp_path: Path) -> None:
    _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))
    changed = _archive(tmp_path / "v2", street_a="SYNTHETIC ELM STREET EXT")

    with pytest.raises(CadImportError, match="different contents"):
        _source(engine).import_archive(ImportRequest(changed, "certified"))
    report = _source(engine).import_archive(ImportRequest(changed, "certified", force=True))

    assert report.status == "loaded"
    assert _parcel(engine, ACCOUNT_A).street_name == "SYNTHETIC ELM STREET EXT"
    assert _count(engine, "parcel_version") == 2


def test_values_free_current_file_keeps_certified_values(engine: Engine, tmp_path: Path) -> None:
    _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))
    current = _archive(
        tmp_path / "v2", "DCAD2026_CURRENT.ZIP", values=False, street_a="SYNTHETIC ELM ST UNIT"
    )

    report = _source(engine).import_archive(ImportRequest(current, "current"))

    parcel = _parcel(engine, ACCOUNT_A)
    assert report.status == "loaded"
    assert parcel.street_name == "SYNTHETIC ELM ST UNIT"
    assert parcel.total_value == Decimal("390000.00")
    with engine.connect() as connection:
        version_values = connection.execute(
            text("SELECT land_value FROM parcel_version WHERE source_file_id = :id"),
            {"id": report.source_file_id},
        ).scalars()
        assert set(version_values) == {None}


def test_older_file_does_not_overwrite_newer_attributes(engine: Engine, tmp_path: Path) -> None:
    source = _source(engine)
    source.import_archive(ImportRequest(_archive(tmp_path), "certified"))
    older = _archive(tmp_path / "v2", street_a="SYNTHETIC OLD NAME")

    source.import_archive(ImportRequest(older, "certified", file_date=date(2026, 1, 1)))

    assert _parcel(engine, ACCOUNT_A).street_name == "SYNTHETIC ELM ST"


def test_missing_mapped_column_fails_the_import_and_marks_it(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from feasibility.snapshot import cad_layout

    header = [c for c in cad_layout.LAND_HEADER if c != "ZONING"]
    monkeypatch.setitem(cad_layout.HEADERS, "LAND", header)

    with pytest.raises(ValueError, match="missing columns"):
        _source(engine).import_archive(ImportRequest(_archive(tmp_path), "certified"))

    with engine.connect() as connection:
        status, error = connection.execute(text("SELECT status, error FROM source_file")).one()
    assert status == "failed"
    assert "ZONING" in error
    assert _count(engine, "parcel") == 0


def test_roll_year_comes_from_the_archive_name(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(CadImportError, match="roll year"):
        _source(engine).import_archive(
            ImportRequest(_archive(tmp_path, "parcels.zip"), "certified")
        )

    report = _source(engine).import_archive(
        ImportRequest(_archive(tmp_path, "parcels.zip"), "certified", roll_year=2026)
    )
    assert report.status == "loaded"


def test_job_archive_paths_stay_inside_the_local_directory(tmp_path: Path) -> None:
    assert resolve_local_file(tmp_path, "DCAD2026_CURRENT.ZIP") == tmp_path / "DCAD2026_CURRENT.ZIP"
    with pytest.raises(ValueError, match="outside"):
        resolve_local_file(tmp_path, "../etc/passwd")


@pytest.mark.slow
def test_a_hundred_megabyte_import_streams_in_bounded_memory(
    engine: Engine, tmp_path: Path
) -> None:
    accounts = 330_000
    archive = tmp_path / "DCAD2026_CURRENT.ZIP"
    write_archive(
        str(archive),
        {
            "ACCOUNT_INFO": (
                _account(f"9{n:016d}", f"SYNTHETIC STREET {n % 900}") for n in range(accounts)
            ),
            "ACCOUNT_APPRL_YEAR": (_values(f"9{n:016d}", 100000, 50000) for n in range(accounts)),
            "RES_DETAIL": (
                {"ACCOUNT_NUM": f"9{n:016d}", "YR_BUILT": "1950", "TOT_LIVING_AREA_SF": "1500"}
                for n in range(accounts)
            ),
            "LAND": (
                {
                    "ACCOUNT_NUM": f"9{n:016d}",
                    "SECTION_NUM": "1",
                    "AREA_SIZE": "7000",
                    "AREA_UOM_DESC": "SQUARE FEET",
                    "ZONING": "R-7.5(A)",
                }
                for n in range(accounts)
            ),
        },
        MEMBER_TIME,
    )

    tracemalloc.start()
    report = _source(engine).import_archive(ImportRequest(archive, "certified"))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert report.rows_loaded == accounts
    assert peak < 50 * 1024 * 1024


def test_account_flagged_after_an_earlier_load_is_removed(engine: Engine, tmp_path: Path) -> None:
    source = _source(engine)
    source.import_archive(ImportRequest(_archive(tmp_path), "certified"))
    later = tmp_path / "v2"
    later.mkdir()
    files = _files()
    files["ACCOUNT_INFO"][0]["EXCLUDE_OWNER"] = "Y"
    write_archive(str(later / "DCAD2026_CURRENT.ZIP"), files, MEMBER_TIME)

    report = source.import_archive(
        ImportRequest(later / "DCAD2026_CURRENT.ZIP", "certified", file_date=date(2026, 10, 1))
    )

    assert report.skip_reasons["removed_flagged_accounts"] == 1
    assert _parcel(engine, ACCOUNT_A) is None
    with engine.connect() as connection:
        versions = connection.execute(
            text("SELECT count(*) FROM parcel_version WHERE account_id = :a"), {"a": ACCOUNT_A}
        ).scalar_one()
    assert versions == 0

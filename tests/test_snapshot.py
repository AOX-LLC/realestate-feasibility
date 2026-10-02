import csv
import importlib.util
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import SecretStr, TypeAdapter
from sqlalchemy import Engine, text

from feasibility.config import DataMode, Settings
from feasibility.snapshot.cad_layout import DO_NOT_IMPORT
from feasibility.snapshot.days import snapshot_day
from feasibility.snapshot.load import LiveModeSeedError, seed
from feasibility.sources.rentcast.models import PropertyRecord, SaleListing, ValueEstimate
from feasibility.sources.rentcast.transport import request_key
from feasibility.sourcing.errors import NoSnapshotForDateError

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "data" / "snapshot"
CAD = SNAPSHOT / "cad" / "dallas"
VALIDATORS: dict[str, TypeAdapter[Any]] = {
    "/listings/sale": TypeAdapter(list[SaleListing]),
    "/listings/sale/{id}": TypeAdapter(SaleListing),
    "/properties": TypeAdapter(list[PropertyRecord]),
    "/avm/value": TypeAdapter(ValueEstimate),
}
OVERLAY = SNAPSHOT / "rentcast" / "day-2"
EXCLUDED_ACCOUNT = "99000000000000060"
CURRENT_ONLY = ("99000000000000061", "99000000000000062", "99000000000000063")
RENAMED_ACCOUNT = "99000000000000015"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_snapshot", REPO_ROOT / "scripts" / "generate_snapshot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rentcast_records(*, base_only: bool = False) -> list[tuple[Path, dict[str, Any]]]:
    """Every recorded response; the day-2 overlay too unless `base_only`."""
    pattern = "*.json" if base_only else "**/*.json"
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((SNAPSHOT / "rentcast").glob(pattern))
    ]


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_every_recorded_response_matches_its_model_and_its_file_name() -> None:
    records = _rentcast_records()

    assert records
    for path, record in records:
        VALIDATORS[record["endpoint"]].validate_python(record["body"])
        assert path.stem == request_key(record["path"], record["params"])
        assert record["status"] == 200


def test_snapshot_holds_the_listings_and_five_detailed_listings() -> None:
    by_endpoint: dict[str, list[Any]] = {}
    for _, record in _rentcast_records(base_only=True):
        by_endpoint.setdefault(record["endpoint"], []).append(record["body"])

    assert len(by_endpoint["/listings/sale"][0]) == 20
    assert {endpoint: len(bodies) for endpoint, bodies in by_endpoint.items()} == {
        "/listings/sale": 1,
        "/listings/sale/{id}": 5,
        "/properties": 5,
        "/avm/value": 5,
    }
    for estimate in by_endpoint["/avm/value"]:
        non_sale = [c for c in estimate["comparables"] if c.get("listingType") != "Standard"]
        assert len(non_sale) >= 2


def test_no_personal_fields_in_any_recorded_response() -> None:
    for path, record in _rentcast_records():
        assert not {"listingAgent", "listingOffice", "owner"} & _keys(record), path.name


def test_regenerating_reproduces_the_committed_snapshot_byte_for_byte(tmp_path: Path) -> None:
    assert _generator().main(["--out", str(tmp_path)]) == 0

    assert _files_under(tmp_path) == _files_under(SNAPSHOT)


def test_cad_csvs_use_crlf_and_quote_every_field() -> None:
    csv_files = sorted(CAD.glob("*/*.CSV"))

    assert len(csv_files) == 8
    for path in csv_files:
        content = path.read_bytes()
        assert content.endswith(b'"\r\n')
        assert b"\n" not in content.replace(b"\r\n", b"")
        for line in content.decode("ascii").split("\r\n")[:-1]:
            assert line.startswith('"')
            assert line.endswith('"')
            assert '","' in line


def _parcel(engine: Engine, account: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT * FROM parcel WHERE market = 'dallas' AND account_id = :a"),
            {"a": account},
        ).first()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def _counts(engine: Engine) -> dict[str, int]:
    return {
        table: _count(engine, table)
        for table in ("parcel", "parcel_version", "source_file", "listing")
    }


def test_seed_loads_parcels_values_and_listings(engine: Engine) -> None:
    report = seed(engine, Settings())

    certified, current = report.certified, report.current
    assert certified is not None and current is not None
    assert (certified.status, current.status) == ("loaded", "loaded")
    assert certified.rows_skipped == 1
    assert _parcel(engine, EXCLUDED_ACCOUNT) is None
    certified_parcel = _parcel(engine, "99000000000000001")
    assert certified_parcel.total_value is not None
    assert certified_parcel.values_file_date == date(2026, 9, 15)
    for account in CURRENT_ONLY:
        added = _parcel(engine, account)
        assert added is not None
        assert (added.land_value, added.improvement_value, added.total_value) == (None,) * 3
        assert added.values_file_date is None
    renamed = _parcel(engine, RENAMED_ACCOUNT)
    certified_rows = csv.DictReader(
        (CAD / "certified" / "ACCOUNT_INFO.CSV").read_bytes().decode("ascii").splitlines()
    )
    certified_name = next(
        row["FULL_STREET_NAME"].strip()
        for row in certified_rows
        if row["ACCOUNT_NUM"] == RENAMED_ACCOUNT
    )
    assert renamed.street_name != certified_name
    assert renamed.street_name.split()[:-1] == certified_name.split()[:-1]
    assert renamed.total_value is not None and renamed.total_value > Decimal(0)
    assert report.listings == len(
        next(
            r["body"]
            for _, r in _rentcast_records(base_only=True)
            if r["endpoint"] == "/listings/sale"
        )
    )
    assert _count(engine, "listing") == report.listings
    assert _count(engine, "parcel") == 70


def test_seed_exercises_the_importer_edge_cases(engine: Engine) -> None:
    seed(engine, Settings())

    two_buildings = _parcel(engine, "99000000000000006")
    assert (two_buildings.living_area_sqft, two_buildings.year_built) == (2070, 1948)
    two_sections = _parcel(engine, "99000000000000008")
    assert two_sections.lot_size_sqft > Decimal(10890)
    assert _parcel(engine, "99000000000000010").lot_size_sqft is None
    vacant = _parcel(engine, "99000000000000051")
    assert (vacant.use_code, vacant.living_area_sqft, vacant.year_built) == ("C11", None, None)
    with engine.connect() as connection:
        joined = connection.execute(
            text(
                "SELECT count(*) FROM listing l JOIN parcel p ON p.market = l.market"
                " AND p.zip5 = l.zip5 AND p.street_number || ' ' || p.street_name"
                " = upper(l.address_line)"
            )
        ).scalar_one()
    assert joined >= 19


def test_seed_is_idempotent(engine: Engine) -> None:
    seed(engine, Settings())
    before = _counts(engine)

    second = seed(engine, Settings())

    assert second.certified is not None and second.current is not None
    assert (second.certified.status, second.current.status) == ("unchanged", "unchanged")
    assert _counts(engine) == before


def test_marker_never_reaches_the_database(engine: Engine) -> None:
    seed(engine, Settings())

    with engine.connect() as connection:
        dumped = connection.execute(
            text(
                "SELECT string_agg(t, ' ') FROM ("
                " SELECT row_to_json(p)::text AS t FROM parcel p"
                " UNION ALL SELECT row_to_json(v)::text FROM parcel_version v) x"
            )
        ).scalar_one()
    assert dumped and DO_NOT_IMPORT not in dumped


@pytest.mark.parametrize("kind", ["certified", "current"])
def test_every_set_carries_the_four_files(kind: str) -> None:
    names = sorted(path.name for path in (CAD / kind).glob("*.CSV"))

    assert names == ["ACCOUNT_APPRL_YEAR.CSV", "ACCOUNT_INFO.CSV", "LAND.CSV", "RES_DETAIL.CSV"]


def test_seed_refuses_a_live_database(engine: Engine) -> None:
    live = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SecretStr("k")
    )

    with pytest.raises(LiveModeSeedError):
        seed(engine, live)

    assert _counts(engine) == {"parcel": 0, "parcel_version": 0, "source_file": 0, "listing": 0}


def test_days_file_lists_both_snapshot_days() -> None:
    recorded = json.loads((SNAPSHOT / "days.json").read_text(encoding="utf-8"))

    assert recorded == {
        "days": [
            {"as_of": "2026-10-01", "overlay": None},
            {"as_of": "2026-10-02", "overlay": "day-2"},
        ],
        "market": "dallas",
    }


def test_snapshot_day_resolves_each_date_and_rejects_others() -> None:
    settings = Settings()

    assert snapshot_day(settings, "dallas", date(2026, 10, 1)) is None
    assert snapshot_day(settings, "dallas", date(2026, 10, 2)) == "day-2"
    with pytest.raises(NoSnapshotForDateError, match="2026-10-01, 2026-10-02"):
        snapshot_day(settings, "dallas", date(2026, 10, 3))


def test_seeded_listings_are_first_seen_on_the_first_snapshot_day(engine: Engine) -> None:
    seed(engine, Settings())

    with engine.connect() as connection:
        stamps = connection.execute(
            text(
                "SELECT min(first_seen_at AT TIME ZONE 'America/Chicago'), "
                "max(last_seen_at AT TIME ZONE 'America/Chicago') FROM listing"
            )
        ).one()
    assert stamps == (datetime(2026, 10, 1, 6, 0), datetime(2026, 10, 1, 6, 0))


def _listings_in(directory: Path) -> list[dict[str, Any]]:
    (feed,) = (
        record["body"]
        for record in (
            json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")
        )
        if record["endpoint"] == "/listings/sale"
    )
    return list(feed)


def test_day_one_listings_were_listed_inside_a_two_day_window() -> None:
    listings = _listings_in(SNAPSHOT / "rentcast")

    assert len(listings) == 20
    assert {listing["listedDate"][:10] for listing in listings} == {"2026-09-30", "2026-10-01"}


def test_day_two_overlay_holds_the_documented_changes() -> None:
    base = {listing["id"]: listing for listing in _listings_in(SNAPSHOT / "rentcast")}
    overlay = {listing["id"]: listing for listing in _listings_in(OVERLAY)}

    assert len(overlay) == 27
    delisted = [i for i in base if i not in overlay and not i.endswith("-r2")]
    assert sorted(delisted) == [
        "6592-Sablewick-Dr,-Dallas,-TX-75218",
        "7059-Ostravelle-Trl,-Dallas,-TX-75220",
    ]
    repriced = {i for i in base if i in overlay and base[i]["price"] != overlay[i]["price"]}
    assert repriced == {"1893-Thistlewane-Dr,-Dallas,-TX-75218"}
    assert overlay["1893-Thistlewane-Dr,-Dallas,-TX-75218"]["price"] == 321000.0
    relisted = overlay["6592-Sablewick-Dr,-Dallas,-TX-75218-r2"]
    assert (relisted["price"], relisted["listedDate"][:10]) == (399000.0, "2026-10-02")
    new = [
        item for item_id, item in overlay.items() if item_id not in base and "-r2" not in item_id
    ]
    assert len(new) == 8
    assert {item["listedDate"][:10] for item in new} == {"2026-10-02"}


def test_overlay_uses_the_same_request_key_as_the_base_feed() -> None:
    base_feeds = [
        path.name
        for path, record in _rentcast_records(base_only=True)
        if record["endpoint"] == "/listings/sale"
    ]

    assert len(base_feeds) == 1
    assert (OVERLAY / base_feeds[0]).is_file()


def test_seed_loads_the_address_cases(engine: Engine) -> None:
    seed(engine, Settings())

    def fields(account: str) -> tuple[Any, ...]:
        row = _parcel(engine, account)
        return (row.street_number, row.street_half, row.street_name, row.unit, row.gis_parcel_id)

    assert fields("99000000000000064") == ("4120", None, "BRINDLECOMBE ST", None, "SYN000063")
    assert fields("99000000000000065") == ("4120", "1/2", "BRINDLECOMBE ST", None, "SYN000064")
    assert fields("99000000000000066")[2] == "N WEXCOMBE AVE"
    assert fields("99000000000000067")[2] == "S WEXCOMBE AVE"
    assert fields("99000000000000068")[3:] == ("101", "SYN000067")
    assert fields("99000000000000069")[3:] == ("102", "SYN000067")
    assert fields("99000000000000070")[4] != fields("99000000000000071")[4]
    assert fields("99000000000000070")[:3] == fields("99000000000000071")[:3]
    assert _parcel(engine, "99000000000000071").land_value == Decimal(200000)


def test_reseeding_after_a_later_day_keeps_the_later_day(engine: Engine) -> None:
    seed(engine, Settings())
    later = "2026-10-02 06:00-05"
    with engine.begin() as connection:
        connection.execute(text("UPDATE listing SET price = 1, last_seen_at = :t"), {"t": later})

    seed(engine, Settings())

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT DISTINCT price, last_seen_at FROM listing")).all()
    assert [(row.price, row.last_seen_at.isoformat()) for row in rows] == [
        (Decimal(1), "2026-10-02T11:00:00+00:00")
    ]

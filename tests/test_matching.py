from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, insert, select

from feasibility.sourcing.keys import parcel_street_key
from feasibility.sourcing.matching import (
    MatchParcel,
    ParcelIndex,
    aggregate,
    build_index,
    load_parcel_index,
    match_listing,
)
from feasibility.sourcing.store import ListingMatch, write_matches
from feasibility.tables import listing, listing_match, parcel

VALUES_DATE = date(2026, 1, 1)


def _parcel(
    account: str,
    number: str,
    name: str,
    zip5: str,
    *,
    half: str = "",
    unit: str | None = None,
    gis: str | None = None,
    land: int | None = 100000,
    total: int | None = 150000,
    lot: int | None = 8000,
) -> MatchParcel:
    key = parcel_street_key(number, half, name)
    assert key is not None
    return MatchParcel(
        account_id=account,
        gis_parcel_id=gis,
        unit=unit,
        zip5=zip5,
        key=key,
        land_value=None if land is None else Decimal(land),
        improvement_value=None if land is None or total is None else Decimal(total - land),
        total_value=None if total is None else Decimal(total),
        year_built=1950,
        lot_size_sqft=None if lot is None else Decimal(lot),
        values_file_date=VALUES_DATE,
    )


TWO_UNITS = {"land": 170000, "total": 230000}

# The synthetic address cases, hand-built.
INDEX: ParcelIndex = build_index(
    [
        _parcel("064", "4120", "BRINDLECOMBE ST", "75214", gis="G63"),
        _parcel("065", "4120", "BRINDLECOMBE ST", "75214", half="1/2", gis="G64"),
        _parcel("066", "5521", "N WEXCOMBE AVE", "75206", gis="G65"),
        _parcel("067", "5521", "S WEXCOMBE AVE", "75206", gis="G66"),
        _parcel(
            "068", "3300", "ORRINMOOR LN", "75209", unit="101", gis="G67", land=170000, total=230000
        ),
        _parcel(
            "069", "3300", "ORRINMOOR LN", "75209", unit="102", gis="G67", land=170000, total=230000
        ),
        _parcel("070", "2200", "KESTRELWYN DR", "75218", gis="G68"),
        _parcel("071", "2200", "KESTRELWYN DR", "75218", gis="G69"),
        _parcel("015", "554", "OSTRAVELLE DR", "75228", gis="G15"),
        _parcel("001", "2737", "THISTLEWANE TRL", "75214", gis="G1"),
        _parcel("200", "700", "ELM ST", "75214", unit="A"),
        _parcel("201", "810", "OAK ST", "75214", gis="G201"),
        _parcel("202", "810", "OAK DR", "75214", gis="G202"),
        _parcel("203", "900", "PINE AVE", "75214", unit="B", gis="G203"),
        _parcel("204", "900", "PINE AVE", "75214", unit="C", gis="G204"),
    ]
)


def _accounts(
    line: str, zip5: str = "75214", unit: str | None = None
) -> tuple[str, str | None, int, list[str]]:
    result = match_listing(INDEX, zip5, line, unit)
    return (
        result.status,
        result.method,
        result.account_count,
        sorted(item.account_id for item in result.parcels),
    )


@pytest.mark.parametrize(
    ("line", "zip5", "unit", "expected"),
    [
        ("2737 THISTLEWANE TRL", "75214", None, ("matched", "exact", 1, ["001"])),
        ("2737 Thistlewane Trail", "75214", None, ("matched", "exact", 1, ["001"])),
        ("554 OSTRAVELLE AVE", "75228", None, ("matched", "stem", 1, ["015"])),
        ("4120 1/2 BRINDLECOMBE ST", "75214", None, ("matched", "exact", 1, ["065"])),
        ("4120 BRINDLECOMBE ST", "75214", None, ("matched", "exact", 1, ["064"])),
        ("5521 S WEXCOMBE AVE", "75206", None, ("matched", "exact", 1, ["067"])),
        ("5521 South Wexcombe Avenue", "75206", None, ("matched", "exact", 1, ["067"])),
        ("5521 N WEXCOMBE AVE", "75206", None, ("matched", "exact", 1, ["066"])),
        # No directional: the stem of "N WEXCOMBE AVE" is "N WEXCOMBE", so nothing matches.
        # A known gap (ARCHITECTURE.md), kept unmatched rather than guessed.
        ("5521 WEXCOMBE AVE", "75206", None, ("unmatched", None, 0, [])),
        ("3300 ORRINMOOR LN #102", "75209", None, ("matched", "exact", 1, ["069"])),
        ("3300 ORRINMOOR LN", "75209", "Unit 101", ("matched", "exact", 1, ["068"])),
        ("3300 ORRINMOOR LN #999", "75209", None, ("unmatched", None, 0, [])),
        ("3300 ORRINMOOR LN", "75209", None, ("matched", "gis_group", 2, ["068", "069"])),
        ("700 ELM ST", "75214", "A", ("matched", "exact", 1, ["200"])),
        ("2200 KESTRELWYN DR", "75218", None, ("ambiguous", None, 2, ["070", "071"])),
        ("900 PINE AVE", "75214", None, ("ambiguous", None, 2, ["203", "204"])),
        ("9100 BRINDLECOMBE ST", "75214", None, ("unmatched", None, 0, [])),
        ("2737 THISTLEWANE TRL", "75206", None, ("unmatched", None, 0, [])),
        ("2737 THISTLEWANE TRL", None, None, ("unmatched", None, 0, [])),
        ("PO BOX 5", "75214", None, ("unmatched", None, 0, [])),
        ("810 OAK AVE", "75214", None, ("ambiguous", None, 2, ["201", "202"])),
    ],
)
def test_match_listing(
    line: str, zip5: str | None, unit: str | None, expected: tuple[str, str | None, int, list[str]]
) -> None:
    assert _accounts(line, zip5, unit) == expected


def test_street_only_matches_a_unit_listing_to_the_one_unitless_parcel() -> None:
    index = build_index([_parcel("300", "50", "ROSE LN", "75214")])

    result = match_listing(index, "75214", "50 ROSE LN", "A")

    assert (result.status, result.method) == ("matched", "street_only")


def test_a_unit_listing_is_unmatched_when_the_only_parcel_has_a_different_unit() -> None:
    index = build_index([_parcel("300", "50", "ROSE LN", "75214", unit="B")])

    assert match_listing(index, "75214", "50 ROSE LN", "A").status == "unmatched"


def test_the_street_key_is_recorded_for_each_outcome() -> None:
    assert match_listing(INDEX, "75214", "9100 BRINDLECOMBE ST", None).street_key == (
        "9100||BRINDLECOMBE ST"
    )
    assert match_listing(INDEX, "75214", "PO BOX 5", None).street_key == "PO BOX 5"


def test_aggregate_of_a_single_parcel_is_its_own_values() -> None:
    result = match_listing(INDEX, "75214", "2737 THISTLEWANE TRL", None)

    matched = aggregate(result)

    assert matched.account_id == "001"
    assert (matched.land_value, matched.total_value) == (Decimal(100000), Decimal(150000))


def test_aggregate_of_a_gis_group_adds_values_and_takes_the_largest_lot() -> None:
    result = match_listing(INDEX, "75209", "3300 ORRINMOOR LN", None)

    matched = aggregate(result)

    assert matched.account_id == "068"
    assert matched.gis_parcel_id == "G67"
    assert matched.land_value == Decimal(340000)
    assert matched.total_value == Decimal(460000)
    assert matched.lot_size_sqft == Decimal(8000)
    assert matched.values_file_date == VALUES_DATE


def test_a_missing_value_in_any_gis_group_account_makes_the_sum_missing() -> None:
    index = build_index(
        [
            _parcel("1", "5", "ASH ST", "75214", gis="G", land=100, total=200),
            _parcel("2", "5", "ASH ST", "75214", gis="G", land=None, total=None),
        ]
    )

    matched = aggregate(match_listing(index, "75214", "5 ASH ST", None))

    assert (matched.land_value, matched.total_value) == (None, None)
    assert matched.lot_size_sqft == Decimal(8000)


def test_aggregate_refuses_an_unmatched_result() -> None:
    with pytest.raises(ValueError, match="unmatched"):
        aggregate(match_listing(INDEX, "75214", "9100 BRINDLECOMBE ST", None))


# --- database


def _insert_parcels(engine: Engine) -> None:
    base = {"market": "dallas", "attrs_file_date": VALUES_DATE}
    rows = [
        {**base, "account_id": "A1", "gis_parcel_id": "G1", "street_number": "10",
         "street_name": "ELM ST", "zip5": "75214", "land_value": Decimal(100)},
        {**base, "account_id": "A2", "street_number": "12", "street_name": "ELM ST",
         "zip5": "75206"},
        {**base, "account_id": "A3", "street_number": "14", "street_name": "ELM ST",
         "zip5": "75209"},
        {**base, "account_id": "A4", "street_number": None, "street_name": "ELM ST",
         "zip5": "75214"},
        {**base, "market": "other", "account_id": "A5", "street_number": "10",
         "street_name": "ELM ST", "zip5": "75214"},
    ]  # fmt: skip
    with engine.begin() as connection:
        for row in rows:
            connection.execute(parcel.insert().values(**row))


def _insert_listing(engine: Engine) -> int:
    with engine.begin() as connection:
        return connection.execute(
            insert(listing)
            .values(
                source="test", external_id="x", market="dallas", address_line="10 ELM ST", raw={}
            )
            .returning(listing.c.id)
        ).scalar_one()


def test_the_index_loads_only_the_requested_zips_and_market(engine: Engine) -> None:
    _insert_parcels(engine)

    with engine.connect() as connection:
        index = load_parcel_index(connection, "dallas", ["75214", "75206"])

    loaded = sorted(item.account_id for found in index.by_key.values() for item in found)
    assert loaded == ["A1", "A2"]  # A3 other zip, A4 no number, A5 other market


def test_write_matches_records_the_match_and_the_account(engine: Engine) -> None:
    _insert_parcels(engine)
    listing_id = _insert_listing(engine)
    with engine.connect() as connection:
        index = load_parcel_index(connection, "dallas", ["75214"])
    result = match_listing(index, "75214", "10 ELM ST", None)

    with engine.begin() as connection:
        write_matches(connection, [ListingMatch(listing_id, result)])
        write_matches(connection, [ListingMatch(listing_id, result)])

    with engine.connect() as connection:
        rows = connection.execute(select(listing_match)).all()
        account = connection.execute(
            select(listing.c.account_id).where(listing.c.id == listing_id)
        ).scalar_one()
    assert len(rows) == 1
    assert (rows[0].status, rows[0].method, rows[0].account_id) == ("matched", "exact", "A1")
    assert (rows[0].gis_parcel_id, rows[0].account_count, rows[0].street_key) == (
        "G1",
        1,
        "10||ELM ST",
    )
    assert account == "A1"


def test_rewriting_a_failed_match_clears_the_account(engine: Engine) -> None:
    _insert_parcels(engine)
    listing_id = _insert_listing(engine)
    with engine.connect() as connection:
        index = load_parcel_index(connection, "dallas", ["75214"])
    with engine.begin() as connection:
        write_matches(
            connection, [ListingMatch(listing_id, match_listing(index, "75214", "10 ELM ST", None))]
        )
        write_matches(
            connection, [ListingMatch(listing_id, match_listing(index, "75214", "99 ELM ST", None))]
        )

    with engine.connect() as connection:
        row = connection.execute(select(listing_match)).one()
        account = connection.execute(select(listing.c.account_id)).scalar_one()
    assert (row.status, row.method, row.account_id, row.account_count) == (
        "unmatched",
        None,
        None,
        0,
    )
    assert account is None


def test_a_stem_hit_never_crosses_units() -> None:
    index = build_index([_parcel("300", "50", "ROSE LN", "75214", unit="B")])

    assert match_listing(index, "75214", "50 ROSE DR", "A").status == "unmatched"
    assert match_listing(index, "75214", "50 ROSE DR", "B").method == "stem"


def test_a_stem_hit_accepts_the_one_unitless_parcel_for_a_unit_listing() -> None:
    index = build_index([_parcel("300", "50", "ROSE LN", "75214")])

    assert match_listing(index, "75214", "50 ROSE DR", "A").method == "stem"

"""Generates the synthetic snapshot committed under data/snapshot/.

    uv run python scripts/generate_snapshot.py [--out DIR]

Everything here is invented: account numbers start 99, street names are made-up words,
there are no people anywhere. The output is deterministic (a fixed seed, no clock, no
environment), so running it twice writes byte-identical files.

Layout written under DIR:
    cad/dallas/certified/*.CSV   a "Certified Data Files" set (carries values)
    cad/dallas/current/*.CSV     a values-free "Most Current Ownership" set
    cad/dallas/manifest.json     roll year and file dates for the two sets
    rentcast/<request key>.json  one recorded RentCast response per request
    rentcast/day-2/<key>.json    the second snapshot day's listing feed (an overlay)
    days.json                    the dates mock mode can source, and each day's overlay

RentCast bodies are built by instantiating the response models and dumping them, so they
cannot drift from the schema the application validates against.
"""

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from feasibility.jobs.handlers import listing_query
from feasibility.markets.loader import get_pack
from feasibility.markets.schema import RentCastListings
from feasibility.snapshot.cad_layout import HEADERS, format_csv, money
from feasibility.sources.rentcast.client import (
    property_record_params,
    sale_listing_path,
    sale_listings_params,
    value_estimate_params,
)
from feasibility.sources.rentcast.models import (
    Comparable,
    Hoa,
    PropertyFeatures,
    PropertyRecord,
    SaleListing,
    SaleListingHistoryEvent,
    SubjectProperty,
    TaxAssessment,
    ValueEstimate,
)
from feasibility.sources.rentcast.transport import request_key

SEED = 20261002
MARKET = "dallas"
ROLL_YEAR = 2026
FILE_DATE = "2026-09-15"
APPRAISAL_YEAR = "2026"
OUTSIDE_BUY_BOX_ZIPS = ("75201", "75240")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "snapshot"

# Invented words, picked to be unlikely as real street names.
STREET_WORDS = (
    "Quillmere", "Bramblecote", "Fenwyck", "Quenderby", "Thistlewane", "Orrinmoor",
    "Valdermere", "Wexcombe", "Ostravelle", "Ashenfold", "Zephrane", "Corvane",
    "Elderquist", "Marlowmere", "Sablewick", "Dunmorrow", "Vintrelow", "Kestrelwyn",
    "Yarrowby", "Ombrelle",
)  # fmt: skip
SUFFIXES = ("ST", "AVE", "DR", "LN", "CT", "PL", "TRL")
RENAMED_SUFFIX = {"ST": "AVE", "AVE": "DR", "DR": "LN", "LN": "PL", "CT": "TRL", "PL": "CT"}

PARCEL_COUNT = 60
TEARDOWNS = range(0, 20)
NEWER_LARGE = range(20, 40)
MIXED = range(40, 50)
VACANT = range(50, 59)
EXCLUDED_OWNER = 59
TWO_BUILDINGS = 5
TWO_LAND_SECTIONS = 7
UNKNOWN_UNIT = 9
RENAMED = 14
OUTSIDE_BUY_BOX = (47, 48)
CURRENT_ONLY_COUNT = 3

LISTED = (0, 1, 2, 3, 4, 5, 6, 8, 10, 11, 12, 20, 21, 22, 23, 40, 41, 47, 50, 51)
DETAILED = (0, 1, 20, 40, 50)
AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
DAY_2 = datetime(2026, 10, 2, tzinfo=UTC)
OVERLAY_DIR = "day-2"
LISTING_WINDOW_START = datetime(2026, 9, 18, tzinfo=UTC)
SALE_LISTING_TYPES_OUT = (None, "Rental")


@dataclass(frozen=True)
class Parcel:
    index: int
    account: str
    street_number: str
    street_word: str
    suffix: str
    zip_code: str
    kind: str  # teardown | newer | mixed | vacant
    land_value: int
    improvement_value: int
    year_built: int
    living_area: int
    lot_sqft: int
    zoning: str
    excluded: bool = False
    street_half: str = ""
    unit: str = ""
    directional: str = ""
    gis: str | None = None

    @property
    def street_name(self) -> str:
        return _street_name(self, self.suffix)

    @property
    def line1(self) -> str:
        return f"{self.street_number} {self.street_word} {self.suffix.title()}"

    @property
    def total_value(self) -> int:
        return self.land_value + self.improvement_value

    @property
    def formatted_address(self) -> str:
        return f"{self.line1}, Dallas, TX {self.zip_code}"

    @property
    def record_id(self) -> str:
        return f"{self.line1},-Dallas,-TX-{self.zip_code}".replace(" ", "-")


def _street_name(parcel: Parcel, suffix: str) -> str:
    prefix = f"{parcel.directional} " if parcel.directional else ""
    return f"{prefix}{parcel.street_word.upper()} {suffix}"


def _round(value: float, step: int = 1000) -> int:
    return int(round(value / step) * step)


def make_parcels(rng: random.Random, buy_box_zips: Sequence[str]) -> list[Parcel]:
    taken: set[tuple[str, str]] = set()
    parcels: list[Parcel] = []
    for index in range(PARCEL_COUNT):
        while True:
            number = str(rng.randint(100, 9899))
            word = rng.choice(STREET_WORDS)
            if (number, word) not in taken:
                taken.add((number, word))
                break
        suffix = rng.choice(SUFFIXES)
        zip_code = (
            OUTSIDE_BUY_BOX_ZIPS[OUTSIDE_BUY_BOX.index(index)]
            if index in OUTSIDE_BUY_BOX
            else buy_box_zips[index % len(buy_box_zips)]
        )
        if index in TEARDOWNS:
            kind, total = "teardown", rng.randint(300, 520) * 1000
            land = _round(total * rng.uniform(0.56, 0.78))
            year, living = rng.randint(1924, 1962), rng.randint(850, 1500)
            lot, zoning = rng.randint(7800, 14500), rng.choice(("R-7.5(A)", "R-5(A)", "R-10(A)"))
        elif index in NEWER_LARGE:
            kind, total = "newer", rng.randint(650, 1250) * 1000
            land = _round(total * rng.uniform(0.2, 0.38))
            year, living = rng.randint(1998, 2024), rng.randint(2400, 4300)
            lot, zoning = rng.randint(6200, 9800), rng.choice(("R-7.5(A)", "R-5(A)", "CD-12"))
        elif index in MIXED:
            kind, total = "mixed", rng.randint(350, 700) * 1000
            land = _round(total * rng.uniform(0.35, 0.54))
            year, living = rng.randint(1966, 1996), rng.randint(1500, 2500)
            lot, zoning = rng.randint(6000, 9000), rng.choice(("R-7.5(A)", "CD-12"))
        else:
            kind, total = "vacant", rng.randint(140, 380) * 1000
            land = total
            year, living = 0, 0
            lot, zoning = rng.randint(5200, 12500), rng.choice(("R-5(A)", "R-7.5(A)", "CD-12"))
        parcels.append(
            Parcel(
                index=index,
                account=f"99{index + 1:015d}",
                street_number=number,
                street_word=word,
                suffix=suffix,
                zip_code=zip_code,
                kind=kind,
                land_value=land,
                improvement_value=total - land if kind != "vacant" else 0,
                year_built=year,
                living_area=living,
                lot_sqft=lot,
                zoning=zoning,
                excluded=index == EXCLUDED_OWNER,
            )
        )
    return parcels


def _extra_accounts(rng: random.Random, buy_box_zips: Sequence[str]) -> list[Parcel]:
    """Accounts only the values-free current set lists (new since the certified roll)."""
    extras = []
    for offset in range(CURRENT_ONLY_COUNT):
        index = PARCEL_COUNT + offset
        extras.append(
            Parcel(
                index=index,
                account=f"99{index + 1:015d}",
                street_number=str(rng.randint(9900, 9999)),
                street_word=rng.choice(STREET_WORDS),
                suffix=rng.choice(SUFFIXES),
                zip_code=buy_box_zips[index % len(buy_box_zips)],
                kind="newer",
                land_value=0,
                improvement_value=0,
                year_built=2026,
                living_area=rng.randint(2200, 3000),
                lot_sqft=rng.randint(6000, 8000),
                zoning="R-7.5(A)",
            )
        )
    return extras


def _case(
    index: int,
    number: str,
    word: str,
    suffix: str,
    zip_code: str,
    values: tuple[int, int, int, int, int],
    zoning: str,
    gis: str,
    **extra: str,
) -> Parcel:
    land, improvement, year_built, lot_sqft, living_area = values
    return Parcel(
        index=index,
        account=f"99{index + 1:015d}",
        street_number=number,
        street_word=word,
        suffix=suffix,
        zip_code=zip_code,
        kind="teardown",
        land_value=land,
        improvement_value=improvement,
        year_built=year_built,
        living_area=living_area,
        lot_sqft=lot_sqft,
        zoning=zoning,
        gis=gis,
        **extra,
    )


# Hand-written (no randomness) parcels that exercise the listing-to-parcel matcher.
# 065 is the half-number twin of 064; 066 and 067 differ only by directional; 068 and 069
# are two accounts on one GIS parcel; 070 and 071 share a situs but not a GIS parcel.
ADDRESS_CASES = (
    _case(63, "4120", "Brindlecombe", "ST", "75214", (235000, 165000, 1954, 7400, 1150),
          "R-7.5(A)", "SYN000063"),
    _case(64, "4120", "Brindlecombe", "ST", "75214", (245000, 125000, 1949, 7600, 1000),
          "R-7.5(A)", "SYN000064", street_half="1/2"),
    _case(65, "5521", "Wexcombe", "AVE", "75206", (215000, 215000, 1962, 6500, 1400),
          "R-5(A)", "SYN000065", directional="N"),
    _case(66, "5521", "Wexcombe", "AVE", "75206", (270000, 150000, 1941, 9100, 1300),
          "R-5(A)", "SYN000066", directional="S"),
    _case(67, "3300", "Orrinmoor", "LN", "75209", (170000, 60000, 1952, 9000, 900),
          "R-7.5(A)", "SYN000067", unit="101"),
    _case(68, "3300", "Orrinmoor", "LN", "75209", (170000, 60000, 1952, 9000, 900),
          "R-7.5(A)", "SYN000067", unit="102"),
    _case(69, "2200", "Kestrelwyn", "DR", "75218", (200000, 100000, 1950, 8000, 1100),
          "R-7.5(A)", "SYN000068"),
    _case(70, "2200", "Kestrelwyn", "DR", "75218", (200000, 100000, 1950, 8000, 1100),
          "R-7.5(A)", "SYN000069"),
)  # fmt: skip


def _account_info(parcel: Parcel, *, renamed: bool) -> dict[str, str]:
    suffix = RENAMED_SUFFIX[parcel.suffix] if renamed and parcel.index == RENAMED else parcel.suffix
    return {
        "ACCOUNT_NUM": parcel.account,
        "APPRAISAL_YR": APPRAISAL_YEAR,
        "DIVISION_CD": "RES",
        "EXCLUDE_OWNER": "Y" if parcel.excluded else "",
        "STREET_NUM": parcel.street_number,
        "STREET_HALF_NUM": parcel.street_half,
        "UNIT_ID": parcel.unit,
        "FULL_STREET_NAME": _street_name(parcel, suffix),
        "PROPERTY_CITY": "DALLAS",
        "PROPERTY_ZIPCODE": f"{parcel.zip_code}{parcel.index:04d}",
        "NBHD_CD": f"SYN{parcel.index % 9:02d}",
        "GIS_PARCEL_ID": parcel.gis or f"SYN{parcel.index:06d}",
    }


def _apprl_year(parcel: Parcel, *, with_values: bool) -> dict[str, str]:
    land = parcel.land_value if with_values else 0
    improvements = parcel.improvement_value if with_values else 0
    return {
        "ACCOUNT_NUM": parcel.account,
        "APPRAISAL_YR": APPRAISAL_YEAR,
        "IMPR_VAL": money(improvements),
        "LAND_VAL": money(land),
        "TOT_VAL": money(land + improvements),
        "SPTD_CODE": "C11" if parcel.kind == "vacant" else "A11",
        "DIVISION_CD": "RES",
    }


def _res_detail(parcel: Parcel) -> list[dict[str, str]]:
    if parcel.kind == "vacant":
        return []
    base = {"ACCOUNT_NUM": parcel.account, "APPRAISAL_YR": APPRAISAL_YEAR}
    if parcel.index != TWO_BUILDINGS:
        return [
            base
            | {
                "YR_BUILT": str(parcel.year_built),
                "TOT_MAIN_SF": str(parcel.living_area),
                "TOT_LIVING_AREA_SF": str(parcel.living_area),
            }
        ]
    # Main house plus a later addition: living area sums, the year comes from the larger.
    return [
        base | {"YR_BUILT": "1948", "TOT_MAIN_SF": "1650", "TOT_LIVING_AREA_SF": "1650"},
        base | {"YR_BUILT": "1985", "TOT_MAIN_SF": "420", "TOT_LIVING_AREA_SF": "420"},
    ]


def _land(parcel: Parcel, *, with_values: bool) -> list[dict[str, str]]:
    sptd = "C11" if parcel.kind == "vacant" else "A11"
    base = {"ACCOUNT_NUM": parcel.account, "APPRAISAL_YR": APPRAISAL_YEAR, "SPTD_CD": sptd}
    land_value = parcel.land_value if with_values else 0

    def row(number: int, size: str, unit: str, value: int) -> dict[str, str]:
        return base | {
            "SECTION_NUM": str(number),
            "ZONING": parcel.zoning,
            "AREA_SIZE": size,
            "AREA_UOM_DESC": unit,
            "VAL_AMT": money(value),
        }

    if parcel.index == TWO_LAND_SECTIONS:
        acres_sqft = 10890  # 0.25 acre
        rest = max(parcel.lot_sqft - acres_sqft, 1500)
        return [
            row(1, ".25", "ACRES", land_value * 3 // 4),
            row(2, str(rest), "SQUARE FEET", land_value - land_value * 3 // 4),
        ]
    if parcel.index == UNKNOWN_UNIT:
        return [row(1, "60", "FRONT FEET", land_value)]
    return [row(1, str(parcel.lot_sqft), "SQUARE FEET", land_value)]


def cad_files(
    parcels: Sequence[Parcel],
    extras: Sequence[Parcel],
    cases: Sequence[Parcel],
    *,
    certified: bool,
) -> dict[str, bytes]:
    accounts = [*parcels, *cases] if certified else [*parcels, *extras, *cases]
    rows: dict[str, list[dict[str, str]]] = {key: [] for key in HEADERS}
    for parcel in accounts:
        rows["ACCOUNT_INFO"].append(_account_info(parcel, renamed=not certified))
        rows["ACCOUNT_APPRL_YEAR"].append(_apprl_year(parcel, with_values=certified))
        rows["RES_DETAIL"].extend(_res_detail(parcel))
        rows["LAND"].extend(_land(parcel, with_values=certified))
    return {f"{key}.CSV": format_csv(HEADERS[key], rows[key]) for key in HEADERS}


def _dump(model: BaseModel) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _listing(parcel: Parcel, rng: random.Random) -> SaleListing:
    is_land = parcel.kind == "vacant"
    # The draw stays so every later price is unchanged; only the date is rebased, to
    # Sep 30 or Oct 1, so the listing sits inside a daysOld=2 window around AS_OF.
    day = rng.randint(18, 29)
    listed = AS_OF - timedelta(days=day % 2)
    markup = rng.uniform(1.04, 1.35)
    price = _round((parcel.land_value if is_land else parcel.total_value) * markup)
    seen = listed.replace(hour=rng.randint(6, 20))
    return SaleListing(
        id=parcel.record_id,
        formatted_address=parcel.formatted_address,
        address_line1=f"{parcel.street_number} {parcel.street_word} {parcel.suffix.title()}",
        city="Dallas",
        state="TX",
        zip_code=parcel.zip_code,
        county="Dallas County",
        latitude=round(32.70 + rng.random() * 0.2, 6),
        longitude=round(-96.90 + rng.random() * 0.2, 6),
        property_type="Land" if is_land else "Single Family",
        bedrooms=None if is_land else float(max(2, parcel.living_area // 650)),
        bathrooms=None if is_land else float(max(1, parcel.living_area // 900)),
        square_footage=None if is_land else parcel.living_area,
        lot_size=float(parcel.lot_sqft),
        year_built=None if is_land else parcel.year_built,
        status="Active",
        price=float(price),
        listing_type="Standard",
        listed_date=listed,
        created_date=listed,
        last_seen_date=seen,
        days_on_market=(AS_OF - listed).days,
        mls_name="SYNTHETIC",
        mls_number=f"SYN{parcel.index + 1:06d}",
    )


def _listing_detail(listing: SaleListing) -> SaleListing:
    listed_date = listing.listed_date or AS_OF
    event = SaleListingHistoryEvent(
        event="Sale Listing",
        price=listing.price,
        listing_type=listing.listing_type,
        listed_date=listed_date,
        days_on_market=listing.days_on_market,
    )
    detail = listing.model_copy(deep=True)
    detail.history = {listed_date.date().isoformat(): event}
    detail.hoa = Hoa(fee=0.0)
    return detail


def _property_record(parcel: Parcel, rng: random.Random) -> PropertyRecord:
    is_land = parcel.kind == "vacant"
    sale_year = rng.randint(1998, 2019)
    return PropertyRecord(
        id=parcel.record_id,
        formatted_address=parcel.formatted_address,
        address_line1=f"{parcel.street_number} {parcel.street_word} {parcel.suffix.title()}",
        city="Dallas",
        state="TX",
        zip_code=parcel.zip_code,
        county="Dallas County",
        property_type="Land" if is_land else "Single Family",
        square_footage=None if is_land else parcel.living_area,
        lot_size=float(parcel.lot_sqft),
        year_built=None if is_land else parcel.year_built,
        assessor_id=parcel.account,
        zoning=parcel.zoning,
        last_sale_date=datetime(sale_year, rng.randint(1, 12), rng.randint(1, 28), tzinfo=UTC),
        last_sale_price=float(_round(parcel.total_value * rng.uniform(0.45, 0.8))),
        features=None
        if is_land
        else PropertyFeatures(
            architecture_type="Ranch" if parcel.year_built < 1970 else "Contemporary",
            cooling=True,
            heating=True,
            floor_count=1 if parcel.living_area < 2000 else 2,
            garage=True,
            garage_spaces=2 if parcel.living_area >= 1500 else 1,
            pool=False,
            roof_type="Composition Shingle",
        ),
        tax_assessments={
            "2025": TaxAssessment(
                year=2025,
                value=float(_round(parcel.total_value * 0.95)),
                land=float(_round(parcel.land_value * 0.95)),
                improvements=float(_round(parcel.improvement_value * 0.95)),
            ),
            "2026": TaxAssessment(
                year=2026,
                value=float(parcel.total_value),
                land=float(parcel.land_value),
                improvements=float(parcel.improvement_value),
            ),
        },
    )


def _value_estimate(
    parcel: Parcel, neighbors: Sequence[Parcel], rng: random.Random
) -> ValueEstimate:
    subject_price = float(_round(parcel.total_value * rng.uniform(1.0, 1.25)))
    comparables = []
    for position, neighbor in enumerate(neighbors):
        # Two comparables per estimate are not sales: one has no listing type, one is a
        # rental. The adapter drops and counts them.
        listing_type = SALE_LISTING_TYPES_OUT[position] if position < 2 else "Standard"
        rental = listing_type == "Rental"
        price = float(_round(neighbor.total_value * rng.uniform(1.0, 1.3)))
        listed = LISTING_WINDOW_START.replace(month=7, day=rng.randint(1, 28))
        comparables.append(
            Comparable(
                id=neighbor.record_id,
                formatted_address=neighbor.formatted_address,
                address_line1=f"{neighbor.street_number} {neighbor.street_word} "
                f"{neighbor.suffix.title()}",
                city="Dallas",
                state="TX",
                zip_code=neighbor.zip_code,
                property_type="Single Family",
                square_footage=neighbor.living_area or None,
                lot_size=float(neighbor.lot_sqft),
                year_built=neighbor.year_built or None,
                status="Inactive",
                price=float(rng.randint(18, 40) * 100) if rental else price,
                listing_type=listing_type,
                listed_date=listed,
                removed_date=listed.replace(month=8),
                days_on_market=rng.randint(15, 70),
                distance=round(rng.uniform(0.2, 1.8), 2),
                days_old=rng.randint(20, 150),
                correlation=round(rng.uniform(0.93, 0.995), 4),
            )
        )
    return ValueEstimate(
        price=subject_price,
        price_range_low=float(_round(subject_price * 0.92)),
        price_range_high=float(_round(subject_price * 1.08)),
        subject_property=SubjectProperty(
            id=parcel.record_id,
            formatted_address=parcel.formatted_address,
            city="Dallas",
            state="TX",
            zip_code=parcel.zip_code,
            property_type="Single Family",
            square_footage=parcel.living_area or None,
            lot_size=float(parcel.lot_sqft),
            year_built=parcel.year_built or None,
        ),
        comparables=comparables,
    )


def _snapshot_record(
    endpoint: str, path: str, params: Mapping[str, str], body: Any
) -> tuple[str, bytes]:
    record = {"endpoint": endpoint, "path": path, "params": dict(params), "status": 200}
    record["body"] = body
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    return f"{request_key(path, params)}.json", text.encode("utf-8")


def make_listings(parcels: Sequence[Parcel], rng: random.Random) -> list[SaleListing]:
    by_index = {parcel.index: parcel for parcel in parcels}
    return [_listing(by_index[index], rng) for index in LISTED]


def _listings_record(listings: Sequence[SaleListing]) -> tuple[str, bytes]:
    pack = get_pack(MARKET)
    spec = next(s for s in pack.sources.listings if isinstance(s, RentCastListings) and s.enabled)
    return _snapshot_record(
        "/listings/sale",
        "/listings/sale",
        sale_listings_params(listing_query(pack, spec)),
        [_dump(listing) for listing in listings],
    )


def rentcast_files(
    parcels: Sequence[Parcel], listings: Sequence[SaleListing], rng: random.Random
) -> dict[str, bytes]:
    by_index = {parcel.index: parcel for parcel in parcels}
    files = dict([_listings_record(listings)])
    houses = [p for p in parcels if p.kind != "vacant" and not p.excluded]
    for index in DETAILED:
        parcel = by_index[index]
        listing = next(item for item in listings if item.id == parcel.record_id)
        files.update(
            [
                _snapshot_record(
                    "/listings/sale/{id}",
                    sale_listing_path(listing.id),
                    {},
                    _dump(_listing_detail(listing)),
                ),
                _snapshot_record(
                    "/properties",
                    "/properties",
                    property_record_params(parcel.formatted_address),
                    [_dump(_property_record(parcel, rng))],
                ),
                _snapshot_record(
                    "/avm/value",
                    "/avm/value",
                    value_estimate_params(parcel.formatted_address),
                    _dump(_value_estimate(parcel, _neighbors(houses, parcel, rng), rng)),
                ),
            ]
        )
    return files


# Day-2 changes to the day-1 feed, by parcel index (account = index + 1).
DELISTED_INDEX = 4  # account 005 disappears
REPRICED_INDEX = 3  # account 004 drops to REPRICED_TO
REPRICED_TO = 321000.0
RELISTED_INDEX = 11  # account 012 returns under a new id at RELISTED_TO
RELISTED_TO = 399000.0
RELIST_ID_SUFFIX = "-r2"

# Address line as written, zip, price, lot sqft, year built (numbered from 1 in order).
NEW_DAY_2_LISTINGS = (
    ("9496 Kestrelwyn Ave", "75230", 345000, 13035, 1945),
    ("1348 Fenwyck Ave", "75206", 355000, 10250, 1958),
    ("554 Ostravelle Ave", "75228", 430000, 11153, 1931),
    ("4120 1/2 Brindlecombe St", "75214", 359000, 7600, 1949),
    ("5521 South Wexcombe Avenue", "75206", 389000, 9100, 1941),
    ("3300 Orrinmoor Ln", "75209", 450000, 9000, 1952),
    ("2200 Kestrelwyn Dr", "75218", 310000, 8000, 1950),
    ("9100 Brindlecombe St", "75214", 295000, 7000, 1948),
)
NEW_LISTING_SQFT = 1100


def _new_day_2_listing(
    number: int, spec: tuple[str, str, int, int, int], rng2: random.Random
) -> SaleListing:
    line1, zip_code, price, lot, year = spec
    return SaleListing(
        id=f"{line1},-Dallas,-TX-{zip_code}".replace(" ", "-"),
        formatted_address=f"{line1}, Dallas, TX {zip_code}",
        address_line1=line1,
        city="Dallas",
        state="TX",
        zip_code=zip_code,
        county="Dallas County",
        latitude=round(32.70 + rng2.random() * 0.2, 6),
        longitude=round(-96.90 + rng2.random() * 0.2, 6),
        property_type="Single Family",
        bedrooms=float(max(2, NEW_LISTING_SQFT // 650)),
        bathrooms=float(max(1, NEW_LISTING_SQFT // 900)),
        square_footage=NEW_LISTING_SQFT,
        lot_size=float(lot),
        year_built=year,
        status="Active",
        price=float(price),
        listing_type="Standard",
        listed_date=DAY_2,
        created_date=DAY_2,
        last_seen_date=DAY_2,
        days_on_market=0,
        mls_name="SYNTHETIC",
        mls_number=f"SYN{100 + number:06d}",
    )


def day2_listing_files(
    parcels: Sequence[Parcel], listings: Sequence[SaleListing], rng2: random.Random
) -> dict[str, bytes]:
    """The second day's /listings/sale response, keyed exactly like day 1's: one account
    delisted, one repriced, one relisted under a new id, and eight new listings."""
    by_index = {parcel.index: parcel for parcel in parcels}
    delisted = by_index[DELISTED_INDEX].record_id
    repriced = by_index[REPRICED_INDEX].record_id
    relisted = by_index[RELISTED_INDEX].record_id
    original = next(listing for listing in listings if listing.id == relisted)
    relist = original.model_copy(
        update={
            "id": f"{relisted}{RELIST_ID_SUFFIX}",
            "price": RELISTED_TO,
            "listed_date": DAY_2,
            "created_date": DAY_2,
            "last_seen_date": DAY_2,
            "days_on_market": 0,
        }
    )
    feed = [
        listing.model_copy(update={"price": REPRICED_TO}) if listing.id == repriced else listing
        for listing in listings
        if listing.id not in (delisted, relisted)
    ]
    feed.append(relist)
    feed.extend(
        _new_day_2_listing(number, spec, rng2)
        for number, spec in enumerate(NEW_DAY_2_LISTINGS, start=1)
    )
    return dict([_listings_record(feed)])


def _neighbors(houses: Sequence[Parcel], subject: Parcel, rng: random.Random) -> list[Parcel]:
    others = [house for house in houses if house.index != subject.index]
    return rng.sample(others, 6)


def write_snapshot(out: Path) -> None:
    rng = random.Random(SEED)
    buy_box = get_pack(MARKET).buy_box.zips
    parcels = make_parcels(rng, buy_box)
    extras = _extra_accounts(rng, buy_box)
    rng2 = random.Random(SEED + 2)  # all new randomness; the original stream is untouched

    cad_dir = out / "cad" / MARKET
    for kind, certified in (("certified", True), ("current", False)):
        directory = cad_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in cad_files(parcels, extras, ADDRESS_CASES, certified=certified).items():
            (directory / name).write_bytes(content)
    manifest = {
        "market": MARKET,
        "roll_year": ROLL_YEAR,
        "sets": {"certified": {"file_date": FILE_DATE}, "current": {"file_date": FILE_DATE}},
    }
    (cad_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    days = {
        "days": [
            {"as_of": AS_OF.date().isoformat(), "overlay": None},
            {"as_of": DAY_2.date().isoformat(), "overlay": OVERLAY_DIR},
        ],
        "market": MARKET,
    }
    (out / "days.json").write_text(
        json.dumps(days, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    rentcast_dir = out / "rentcast"
    overlay_dir = rentcast_dir / OVERLAY_DIR
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*rentcast_dir.glob("*.json"), *overlay_dir.glob("*.json")]:
        stale.unlink()
    listings = make_listings(parcels, rng)
    for name, content in rentcast_files(parcels, listings, rng).items():
        (rentcast_dir / name).write_bytes(content)
    for name, content in day2_listing_files(parcels, listings, rng2).items():
        (overlay_dir / name).write_bytes(content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic snapshot.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: data/snapshot")
    args = parser.parse_args(argv)
    write_snapshot(args.out)
    print(f"wrote snapshot to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Match a listing to the county parcel(s) it is for.

Nothing is guessed: a listing that fits no parcel is `unmatched`, one that fits several
unrelated parcels is `ambiguous`, and both are kept and counted. The rules run in order and
the first that applies wins (see `match_listing`).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import Connection, select

from feasibility.domain.address import normalize_street
from feasibility.sourcing.keys import (
    StreetKey,
    full_key,
    normalize_unit,
    parcel_street_key,
    parse_listing_street,
    stem_key,
)
from feasibility.tables import parcel

MatchStatus = Literal["matched", "ambiguous", "unmatched"]


@dataclass(frozen=True)
class MatchParcel:
    account_id: str
    gis_parcel_id: str | None
    unit: str | None
    zip5: str
    key: StreetKey
    land_value: Decimal | None
    improvement_value: Decimal | None
    total_value: Decimal | None
    year_built: int | None
    lot_size_sqft: Decimal | None
    values_file_date: date | None


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    method: str | None
    parcels: tuple[MatchParcel, ...]
    account_count: int
    street_key: str


@dataclass(frozen=True)
class MatchedParcel:
    """The values of the matched parcel, or of a GIS group's accounts combined."""

    account_id: str
    gis_parcel_id: str | None
    land_value: Decimal | None
    improvement_value: Decimal | None
    total_value: Decimal | None
    year_built: int | None
    lot_size_sqft: Decimal | None
    values_file_date: date | None


@dataclass(frozen=True)
class ParcelIndex:
    by_key: dict[tuple[str, str], list[MatchParcel]]
    by_stem: dict[tuple[str, str], list[MatchParcel]]


def build_index(parcels: Iterable[MatchParcel]) -> ParcelIndex:
    by_key: defaultdict[tuple[str, str], list[MatchParcel]] = defaultdict(list)
    by_stem: defaultdict[tuple[str, str], list[MatchParcel]] = defaultdict(list)
    for item in parcels:
        by_key[(item.zip5, full_key(item.key))].append(item)
        by_stem[(item.zip5, stem_key(item.key))].append(item)
    return ParcelIndex(dict(by_key), dict(by_stem))


def load_parcel_index(connection: Connection, market: str, zips: Sequence[str]) -> ParcelIndex:
    """Index the market's parcels in the given zips: one SELECT, using ix_parcel_market_zip5.

    Parcels with no usable situs or zip cannot be matched and are left out.
    """
    rows = connection.execute(
        select(
            parcel.c.account_id,
            parcel.c.gis_parcel_id,
            parcel.c.unit,
            parcel.c.zip5,
            parcel.c.street_number,
            parcel.c.street_half,
            parcel.c.street_name,
            parcel.c.land_value,
            parcel.c.improvement_value,
            parcel.c.total_value,
            parcel.c.year_built,
            parcel.c.lot_size_sqft,
            parcel.c.values_file_date,
        ).where(parcel.c.market == market, parcel.c.zip5.in_(list(zips)))
    )
    candidates = []
    for row in rows:
        key = parcel_street_key(row.street_number, row.street_half, row.street_name)
        if key is None or row.zip5 is None:
            continue
        candidates.append(
            MatchParcel(
                account_id=row.account_id,
                gis_parcel_id=row.gis_parcel_id,
                unit=normalize_unit(row.unit),
                zip5=row.zip5,
                key=key,
                land_value=row.land_value,
                improvement_value=row.improvement_value,
                total_value=row.total_value,
                year_built=row.year_built,
                lot_size_sqft=row.lot_size_sqft,
                values_file_date=row.values_file_date,
            )
        )
    return build_index(candidates)


def _matched(method: str, found: Sequence[MatchParcel], street_key: str) -> MatchResult:
    return MatchResult("matched", method, tuple(found), len(found), street_key)


def _ambiguous(rivals: Sequence[MatchParcel], street_key: str) -> MatchResult:
    return MatchResult("ambiguous", None, tuple(rivals), len(rivals), street_key)


def _unmatched(street_key: str) -> MatchResult:
    return MatchResult("unmatched", None, (), 0, street_key)


def _match_exact(exact: Sequence[MatchParcel], unit: str | None, street_key: str) -> MatchResult:
    if unit is not None:
        same = [item for item in exact if item.unit == unit]
        if len(same) == 1:
            return _matched("exact", same, street_key)
        if same:
            return _ambiguous(same, street_key)
        if len(exact) == 1 and exact[0].unit is None:
            return _matched("street_only", exact, street_key)
        return _unmatched(street_key)
    if len(exact) == 1:
        return _matched("exact", exact, street_key)
    shared = {item.gis_parcel_id for item in exact}
    if len(shared) == 1 and None not in shared:
        return _matched("gis_group", exact, street_key)
    return _ambiguous(exact, street_key)


def match_listing(
    index: ParcelIndex, zip5: str | None, address_line: str, unit: str | None
) -> MatchResult:
    """Match one listing. The first rule that applies wins:

    1. No zip, or an address with no street number: unmatched.
    2. Parcels with the same street key (number, half, name with canonical suffix) in the
       zip: judged by unit, then by whether several accounts share one GIS parcel.
    3. Otherwise parcels with the same stem (the street without its suffix): exactly one
       is a match, several are ambiguous, none is unmatched.
    """
    parsed = parse_listing_street(address_line, unit)
    if zip5 is None or parsed is None:
        return _unmatched(normalize_street(address_line))
    key, listing_unit = parsed
    street_key = full_key(key)
    exact = index.by_key.get((zip5, street_key))
    if exact:
        return _match_exact(exact, listing_unit, street_key)
    stem = index.by_stem.get((zip5, stem_key(key)), [])
    accounts = {item.account_id for item in stem}
    if len(accounts) == 1:
        return _matched("stem", stem, street_key)
    if accounts:
        return _ambiguous(stem, street_key)
    return _unmatched(street_key)


def _sum(values: Sequence[Decimal | None]) -> Decimal | None:
    return None if None in values else sum((v for v in values if v is not None), Decimal(0))


def aggregate(result: MatchResult) -> MatchedParcel:
    """The values a matched listing is scored on. A GIS group's accounts are one lot:
    values add up (a missing value in any account makes the sum missing), the lot is the
    largest, the year the earliest, the representative account the smallest."""
    if result.status != "matched":
        raise ValueError(f"cannot aggregate a {result.status} match")
    group = result.parcels
    dates = [item.values_file_date for item in group if item.values_file_date is not None]
    years = [item.year_built for item in group if item.year_built is not None]
    lots = [item.lot_size_sqft for item in group if item.lot_size_sqft is not None]
    return MatchedParcel(
        account_id=min(item.account_id for item in group),
        gis_parcel_id=group[0].gis_parcel_id,
        land_value=_sum([item.land_value for item in group]),
        improvement_value=_sum([item.improvement_value for item in group]),
        total_value=_sum([item.total_value for item in group]),
        year_built=min(years) if years else None,
        lot_size_sqft=max(lots) if lots else None,
        values_file_date=min(dates) if dates else None,
    )

"""Read the database and build the pro-forma inputs of a run's ranked candidates.

No engine logic lives here: this maps stored rows to `ProformaInputs` and nothing else. Three
queries serve the whole run (the candidates with their primary listing, their parcels, their
latest estimates), however many candidates it ranked.

A candidate whose primary listing has no positive price in the run cannot be modelled and gets
no inputs: the engine refuses such a price rather than pricing a house at nothing.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, and_, or_, select

from feasibility.markets.schema import normalise_zoning
from feasibility.proforma.model import Comp, EstimateInput, ProformaInputs
from feasibility.sourcing import estimate_store
from feasibility.sourcing.estimate_store import StoredEstimate
from feasibility.sourcing.filters import ListingFacts, is_vacant, lot_size_of
from feasibility.sourcing.matching import MatchedParcel
from feasibility.tables import candidate, listing, parcel, run_candidate, run_listing

log = logging.getLogger(__name__)

GIS_GROUP_METHOD = "gis_group"


@dataclass(frozen=True, slots=True)
class CandidateInputs:
    candidate_id: int
    rank: int
    inputs: ProformaInputs


@dataclass(frozen=True, slots=True)
class _RankedCandidate:
    candidate_id: int
    rank: int
    gis_parcel_id: str | None
    zip5: str | None
    price: Decimal | None
    match_method: str | None
    match_account_id: str | None
    facts: ListingFacts


@dataclass(frozen=True, slots=True)
class _ParcelRow:
    account_id: str
    gis_parcel_id: str | None
    zip5: str | None
    living_area_sqft: int | None
    lot_size_sqft: Decimal | None
    year_built: int | None
    zoning: str | None


def gather_inputs(
    connection: Connection, market: str, run_id: int, as_of: date
) -> list[CandidateInputs]:
    """Inputs for every ranked candidate of the run, in rank order."""
    ranked = _ranked_candidates(connection, run_id)
    parcels = _parcels(connection, market, ranked)
    estimates = estimate_store.latest_estimates(connection, [c.candidate_id for c in ranked], as_of)

    gathered = []
    for item in ranked:
        if item.price is None or item.price <= 0:
            log.warning(
                "candidate %s has no positive price in run %s; no pro-forma",
                item.candidate_id,
                run_id,
            )
            continue
        gathered.append(
            CandidateInputs(
                item.candidate_id,
                item.rank,
                _inputs_of(
                    item,
                    item.price,
                    _parcels_of(item, parcels),
                    estimates.get(item.candidate_id),
                    as_of,
                ),
            )
        )
    return gathered


def _ranked_candidates(connection: Connection, run_id: int) -> list[_RankedCandidate]:
    """The run's ranked candidates with the price and match of their primary listing in that run."""
    rows = connection.execute(
        select(
            run_candidate.c.candidate_id,
            run_candidate.c.rank,
            candidate.c.gis_parcel_id,
            candidate.c.zip5,
            run_listing.c.price,
            run_listing.c.match_method,
            run_listing.c.match_account_id,
            listing.c.property_type,
            listing.c.lot_size_sqft,
            listing.c.year_built,
        )
        .select_from(
            run_candidate.join(candidate, candidate.c.id == run_candidate.c.candidate_id)
            .join(listing, listing.c.id == run_candidate.c.primary_listing_id)
            .join(
                run_listing,
                and_(
                    run_listing.c.run_id == run_candidate.c.run_id,
                    run_listing.c.listing_id == run_candidate.c.primary_listing_id,
                ),
            )
        )
        .where(run_candidate.c.run_id == run_id, run_candidate.c.status == "ranked")
        .order_by(run_candidate.c.rank)
    )
    return [
        _RankedCandidate(
            candidate_id=row.candidate_id,
            rank=row.rank,
            gis_parcel_id=row.gis_parcel_id,
            zip5=row.zip5,
            price=row.price,
            match_method=row.match_method,
            match_account_id=row.match_account_id,
            facts=ListingFacts(
                zip5=row.zip5,
                property_type=row.property_type,
                price=row.price,
                lot_size_sqft=row.lot_size_sqft,
                year_built=row.year_built,
            ),
        )
        for row in rows
    ]


def _is_group(item: _RankedCandidate) -> bool:
    return item.match_method == GIS_GROUP_METHOD and item.gis_parcel_id is not None


def _parcels(
    connection: Connection, market: str, ranked: Sequence[_RankedCandidate]
) -> list[_ParcelRow]:
    """The parcel rows of every candidate: the matched account, or all accounts of a group."""
    accounts = sorted(
        {c.match_account_id for c in ranked if c.match_account_id and not _is_group(c)}
    )
    groups = sorted({c.gis_parcel_id for c in ranked if c.gis_parcel_id and _is_group(c)})
    wanted = []
    if accounts:
        wanted.append(parcel.c.account_id.in_(accounts))
    if groups:
        wanted.append(parcel.c.gis_parcel_id.in_(groups))
    if not wanted:
        return []
    rows = connection.execute(
        select(
            parcel.c.account_id,
            parcel.c.gis_parcel_id,
            parcel.c.zip5,
            parcel.c.living_area_sqft,
            parcel.c.lot_size_sqft,
            parcel.c.year_built,
            parcel.c.zoning,
        ).where(parcel.c.market == market, or_(*wanted))
    )
    return [_ParcelRow(**row._mapping) for row in rows]


def _parcels_of(item: _RankedCandidate, parcels: Sequence[_ParcelRow]) -> list[_ParcelRow]:
    if _is_group(item):
        return [p for p in parcels if p.gis_parcel_id == item.gis_parcel_id and p.zip5 == item.zip5]
    return [p for p in parcels if item.match_account_id and p.account_id == item.match_account_id]


def _inputs_of(
    item: _RankedCandidate,
    price: Decimal,
    parcels: Sequence[_ParcelRow],
    estimate: StoredEstimate | None,
    as_of: date,
) -> ProformaInputs:
    matched = _as_matched(item, parcels)
    zoning, seen = _zoning(parcels)
    return ProformaInputs(
        price=price,
        lot_sqft=lot_size_of(item.facts, matched),
        lot_source="parcel" if matched.lot_size_sqft is not None else "listing",
        zoning=zoning,
        zoning_values_seen=seen,
        is_vacant=is_vacant(item.facts, matched),
        is_gis_group=_is_group(item),
        existing_living_sqft=_existing_area(parcels),
        as_of=as_of,
        estimate=None if estimate is None else _estimate_input(estimate),
    )


def _as_matched(item: _RankedCandidate, parcels: Sequence[_ParcelRow]) -> MatchedParcel:
    """The parcel as the filters see it: the earliest year and the largest lot of a group."""
    years = [p.year_built for p in parcels if p.year_built is not None]
    return MatchedParcel(
        account_id=item.match_account_id or "",
        gis_parcel_id=item.gis_parcel_id,
        land_value=None,
        improvement_value=None,
        total_value=None,
        year_built=min(years) if years else None,
        lot_size_sqft=_parcel_lot(parcels),
        values_file_date=None,
    )


def _parcel_lot(parcels: Sequence[_ParcelRow]) -> Decimal | None:
    lots = [p.lot_size_sqft for p in parcels if p.lot_size_sqft is not None]
    # Accounts on one lot report the same lot; counting it once is the largest, not the sum.
    return max(lots) if lots else None


def _zoning(parcels: Sequence[_ParcelRow]) -> tuple[str | None, tuple[str, ...]]:
    """The zoning the accounts share (None when they disagree or none is known), and every
    distinct value seen. Spellings that differ only in case or spaces are one zoning."""
    seen: dict[str, str] = {}
    for item in parcels:
        if item.zoning and item.zoning.strip():
            seen.setdefault(normalise_zoning(item.zoning), item.zoning.strip())
    values = tuple(sorted(seen.values()))
    return (values[0] if len(values) == 1 else None), values


def _existing_area(parcels: Sequence[_ParcelRow]) -> Decimal | None:
    """The living area standing on the lot: the accounts' areas added up; None when unknown."""
    areas = [p.living_area_sqft for p in parcels if p.living_area_sqft is not None]
    return Decimal(sum(areas)) if areas else None


def _estimate_input(stored: StoredEstimate) -> EstimateInput:
    return EstimateInput(
        fetched_on=stored.fetched_on,
        outcome="ok" if stored.outcome == "ok" else "no_estimate",
        price=stored.price,
        comps=_comps(stored.comps),
    )


def _comps(stored: Sequence[Mapping[str, Any]]) -> tuple[Comp, ...]:
    """Stored comps as the engine's `Comp`: only its own fields (a stored comp also carries
    days_old), and never a comp that sold for nothing."""
    comps = []
    for entry in stored:
        price = Decimal(str(entry["price"]))
        if price <= 0:
            continue
        distance = entry.get("distance_miles")
        comps.append(
            Comp(
                address=entry["address"],
                price=price,
                living_area_sqft=entry.get("living_area_sqft"),
                distance_miles=None if distance is None else Decimal(str(distance)),
                year_built=entry.get("year_built"),
            )
        )
    return tuple(comps)

"""Loaders for the synthetic RESO records, the eval-only records and the answer key, shared by
the data tests and by the standalone checker the data authors run."""

import json
from pathlib import Path
from typing import Any

from feasibility.listings import listing_query
from feasibility.markets.loader import get_pack
from feasibility.sources.mls.reso import IngestedRemarks, ingest_remarks
from feasibility.sources.rentcast.client import sale_listings_params
from feasibility.sources.rentcast.transport import request_key

REPO = Path(__file__).resolve().parents[1]
MLS_FILE = REPO / "data" / "mls" / "dallas.json"
EXTRA_FILE = REPO / "evals" / "signals" / "extra.json"
KEY_FILE = REPO / "evals" / "signals" / "answer_key.json"
SNAPSHOT = REPO / "data" / "snapshot"

# Snapshot listing ids that the plan names (account = the digits after SYN).
DEMO_SIGNALS = {
    "SYN000051": {"plans_or_permits", "protected_trees"},
    "SYN000004": {"teardown_language", "as_is_sale"},
    "SYN000052": {"deed_restrictions", "flood_or_drainage"},
    "SYN000002": {"tenant_occupied", "conservation_or_historic_district"},
    "SYN000006": {"environmental_hazard", "easement_or_encroachment"},
    "SYN000103": {"seller_financing", "teardown_language"},
}
NULL_REMARKS = {"SYN000009", "SYN000013"}
NEGATIONS_ONLY = "SYN000007"
INJECTED_SNAPSHOT = {"SYN000103", NEGATIONS_ONLY}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def feed_listings() -> dict[str, dict[str, Any]]:
    """The snapshot feeds' listings by `mlsNumber`: day 2 first, then day 1 over it, so a
    listing in both days is the day-1 version."""
    pack = get_pack("dallas")
    params = sale_listings_params(listing_query(pack, pack.sources.listings[0]))
    name = f"{request_key('/listings/sale', params)}.json"
    listings: dict[str, dict[str, Any]] = {}
    for path in (SNAPSHOT / "rentcast" / "day-2" / name, SNAPSHOT / "rentcast" / name):
        for listing in read_json(path)["body"]:
            listings[listing["mlsNumber"]] = listing
    return listings


def feed_row_ids() -> dict[str, str]:
    """Every distinct listing row of both feeds (by RentCast id) and its `mlsNumber`. A
    relisted property is two rows with one number."""
    pack = get_pack("dallas")
    params = sale_listings_params(listing_query(pack, pack.sources.listings[0]))
    name = f"{request_key('/listings/sale', params)}.json"
    rows: dict[str, str] = {}
    for path in (SNAPSHOT / "rentcast" / "day-2" / name, SNAPSHOT / "rentcast" / name):
        for listing in read_json(path)["body"]:
            rows[listing["id"]] = listing["mlsNumber"]
    return rows


def snapshot_records() -> list[dict[str, Any]]:
    return list(read_json(MLS_FILE))


def extra_records() -> list[dict[str, Any]]:
    return list(read_json(EXTRA_FILE))


def answer_key() -> dict[str, dict[str, Any]]:
    return dict(read_json(KEY_FILE))


def all_records() -> list[dict[str, Any]]:
    return [*snapshot_records(), *extra_records()]


def ingested(record: dict[str, Any]) -> IngestedRemarks | None:
    return ingest_remarks(record["PublicRemarks"])


def redacted_text(record: dict[str, Any]) -> str:
    found = ingested(record)
    return "" if found is None else found.text


def decode_tag_characters(text: str) -> str:
    """Text hidden in the Unicode tag block (U+E0020..U+E007E), as the ASCII it spells."""
    return "".join(chr(ord(c) - 0xE0000) for c in text if 0xE0020 <= ord(c) <= 0xE007E)

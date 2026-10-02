"""Street keys: the normalized forms two sources' spellings of one address compare on.

These sit on top of `domain.address.normalize_street`, which is not changed. A key splits an
address into number, half number and street name, with the street-type suffix canonicalized
(TRAIL, TR and TRL are one suffix). The *stem* is the name without its suffix, used only as
a fallback for a street whose suffix differs between sources.
"""

import re
from dataclasses import dataclass

from feasibility.domain.address import normalize_street

SUFFIX_ALIASES = {
    "STR": "ST", "STREET": "ST", "AV": "AVE", "AVENUE": "AVE", "AVEN": "AVE",
    "BLV": "BLVD", "BOULEVARD": "BLVD", "CRT": "CT", "COURT": "CT", "DRV": "DR",
    "DRIVE": "DR", "LANE": "LN", "PLC": "PL", "PLACE": "PL", "ROAD": "RD", "TR": "TRL",
    "TRAIL": "TRL", "TRAILS": "TRL", "TERR": "TER", "TERRACE": "TER", "PKY": "PKWY",
    "PARKWAY": "PKWY", "CIRC": "CIR", "CIRCLE": "CIR", "HIGHWAY": "HWY",
    "FREEWAY": "FWY", "EXPRESSWAY": "EXPY", "EXPWY": "EXPY", "WY": "WAY",
}  # fmt: skip
SUFFIXES = frozenset(SUFFIX_ALIASES.values()) | {
    "ST", "AVE", "BLVD", "CT", "DR", "LN", "PL", "RD", "TRL", "TER", "PKWY", "CIR", "HWY",
    "FWY", "EXPY", "WAY",
}  # fmt: skip
DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})
UNIT_MARKERS = frozenset({"UNIT", "APT", "APARTMENT", "STE", "SUITE", "#", "NO"})

LISTING_STREET = re.compile(r"^(?P<num>\d+[A-Z]?)(?: (?P<half>\d/\d))? (?P<rest>.+)$")


@dataclass(frozen=True, slots=True)
class StreetKey:
    number: str
    half: str
    name: str
    stem: str


def _canonical(words: list[str]) -> tuple[str, str]:
    """The street name with its suffix canonicalized, and the same name without the suffix.

    The suffix is the last word, or the last word before a trailing directional. Only that
    position is aliased, so a leading ST (ST MARYS DR) is left alone. Directionals stay
    where they are.
    """
    position = len(words) - 1
    if position >= 1 and words[position] in DIRECTIONALS:
        position -= 1
    is_suffix = position >= 1 and (words[position] in SUFFIX_ALIASES or words[position] in SUFFIXES)
    if not is_suffix:
        name = " ".join(words)
        return name, name
    canonical = [*words[:position], SUFFIX_ALIASES.get(words[position], words[position])]
    canonical += words[position + 1 :]
    stem = [*words[:position], *words[position + 1 :]]
    return " ".join(canonical), " ".join(stem)


def normalize_unit(text: str | None) -> str | None:
    """'Unit 102', '#102' and 'APT 102' all become '102'; blank becomes None."""
    if text is None:
        return None
    words = [word.lstrip("#") for word in normalize_street(text).split()]
    kept = [word for word in words if word and word not in UNIT_MARKERS]
    return "".join(kept) or None


def _peel_unit(tokens: list[str]) -> tuple[list[str], str | None]:
    """Split a trailing '#4B', '# 4B', 'UNIT 4B', 'APT 4B' or 'STE 4B' off the street words."""
    if len(tokens) >= 2 and tokens[-1].startswith("#") and len(tokens[-1]) > 1:
        return tokens[:-1], tokens[-1]
    if len(tokens) >= 3 and tokens[-2] in UNIT_MARKERS - {"NO"}:
        return tokens[:-2], tokens[-1]
    return tokens, None


def parse_listing_street(
    address_line: str, unit: str | None
) -> tuple[StreetKey, str | None] | None:
    """The street key and normalized unit of a listing's address line, or None when the line
    does not start with a street number ('PO BOX 5', '')."""
    parsed = LISTING_STREET.match(normalize_street(address_line))
    if parsed is None:
        return None
    tokens = parsed.group("rest").split()
    if unit is None:
        tokens, unit = _peel_unit(tokens)
    name, stem = _canonical(tokens)
    return StreetKey(parsed.group("num"), parsed.group("half") or "", name, stem), normalize_unit(
        unit
    )


def parcel_street_key(
    street_number: str | None, street_half: str | None, street_name: str | None
) -> StreetKey | None:
    """The street key of a parcel's situs, or None when the number or name is blank."""
    number = (street_number or "").strip()
    words = normalize_street(street_name or "").split()
    if not number or not words:
        return None
    name, stem = _canonical(words)
    return StreetKey(number, (street_half or "").strip().upper(), name, stem)


def full_key(key: StreetKey) -> str:
    return f"{key.number}|{key.half}|{key.name}"


def stem_key(key: StreetKey) -> str:
    return f"{key.number}|{key.half}|{key.stem}"


def property_key(
    account_id: str | None,
    gis_parcel_id: str | None,
    method: str | None,
    zip5: str | None,
    key: StreetKey,
    unit: str | None,
) -> str:
    """The identity of a property across listings, sources and days.

    A matched property is its account, or its GIS parcel when several accounts share one.
    An unmatched one is its address, so a later parcel import can upgrade the key.
    """
    if method == "gis_group":
        return f"gis:{gis_parcel_id}"
    if method is not None:
        return f"acct:{account_id}"
    suffix = f":{unit}" if unit else ""
    return f"addr:{zip5 or ''}:{full_key(key)}{suffix}"

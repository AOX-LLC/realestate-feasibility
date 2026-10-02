"""US street addresses and the normalization that lets two sources' spellings compare equal."""

import re

from pydantic import BaseModel, ConfigDict

# USPS Publication 28 abbreviations for the suffixes and directionals seen most often.
STREET_WORDS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "CIRCLE": "CIR",
    "COURT": "CT",
    "DRIVE": "DR",
    "FREEWAY": "FWY",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "PLACE": "PL",
    "ROAD": "RD",
    "STREET": "ST",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "WAY": "WAY",
}
NON_ADDRESS_CHARACTERS = re.compile(r"[^A-Z0-9 #/-]")
ZIP_DIGITS = re.compile(r"\d{5}")


def normalize_street(text: str) -> str:
    """Upper-case, strip punctuation, collapse spaces and abbreviate suffixes and
    directionals: ' 123  North Elm Street. ' -> '123 N ELM ST'."""
    cleaned = NON_ADDRESS_CHARACTERS.sub(" ", text.upper())
    return " ".join(STREET_WORDS.get(word, word) for word in cleaned.split())


def zip5(raw: str | None) -> str | None:
    """The 5-digit ZIP from a 5- or 9-digit value ('752141234', '75214-1234')."""
    if raw is None:
        return None
    match = ZIP_DIGITS.match(raw.strip())
    return match.group(0) if match else None


def _normalized_or_none(text: str | None) -> str | None:
    return normalize_street(text) or None if text else None


class Address(BaseModel):
    model_config = ConfigDict(frozen=True)

    street: str
    unit: str | None = None
    city: str | None = None
    state: str | None = None
    zip5: str | None = None

    @classmethod
    def normalized(
        cls,
        street: str,
        *,
        unit: str | None = None,
        city: str | None = None,
        state: str | None = None,
        zip_code: str | None = None,
    ) -> "Address":
        return cls(
            street=normalize_street(street),
            unit=_normalized_or_none(unit),
            city=_normalized_or_none(city),
            state=_normalized_or_none(state),
            zip5=zip5(zip_code),
        )

    @property
    def one_line(self) -> str:
        street = f"{self.street} {self.unit}" if self.unit else self.street
        locality = " ".join(part for part in (self.state, self.zip5) if part)
        return ", ".join(part for part in (street, self.city, locality) if part)

"""Removes personal data from listing remarks at ingestion, before the domain model exists.

The patterns are a heuristic, and they are the only thing standing between a listing agent's
contact details and the database. They catch the forms the synthetic set plants: a cue word
followed by a name or number, emails (including "name at example dot com"), phone numbers,
links, honorific names, brokerage names and licence numbers. A bare first name with no cue
("Maria will meet you there") passes; docs/ARCHITECTURE.md states that gap and the residual
eval set measures it.

Each rule runs on the output of the one before it, in the order of `_RULES` (the plan's
seven, with the brokerage rule first). Redacting
already-redacted text changes nothing: the replacement token is never a cue.
"""

import re
from dataclasses import dataclass

REMOVED = "[contact removed]"

_CUES = (
    r"call|text|contact|e-?mail|ask\s+for|reach|listed\s+by|listing\s+agent|agent|broker(?:age)?"
    r"|showings?\s+(?:by|with|through)|co-?list(?:ed)?\s+(?:with|by)|presented\s+by"
    r"|courtesy\s+of|represented\s+by"
)
_HONORIFIC_ABBREVIATIONS = ("Mr", "Mrs", "Ms", "Mx", "Dr", "Prof")
# One character of a clause. A clause ends at ! ? ; an em dash, a newline, a spaced hyphen or
# en dash, or a full stop followed by whitespace or the end ("214.555.0187" and "a.com" stay
# inside; "Mr." does not end it).
_CLAUSE_CHAR = (
    r"(?:(?!\s[-\u2013]\s)[^.!?;\n\u2014]"
    r"|\.(?![ \t\r\n]|$)"
    + "".join(rf"|(?<=\b{abbreviation})\." for abbreviation in _HONORIFIC_ABBREVIATIONS)
    + ")"
)
# The "[" guard keeps the replacement token's own word "contact" from being a cue.
_CUE_CLAUSE = re.compile(rf"(?<!\[)\b(?:{_CUES})\b{_CLAUSE_CHAR}*", re.IGNORECASE)

_EMAIL_WORD = r"[\w.+-]+"
_DOMAIN_LABEL = r"[\w-]+"
_EMAILS = (
    re.compile(rf"{_EMAIL_WORD}@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+"),
    # name [at] example [dot] com, name (at) example (dot) com, name{at}example.com
    re.compile(
        rf"{_EMAIL_WORD}\s*[\[({{]\s*at\s*[\])}}]\s*{_DOMAIN_LABEL}"
        rf"(?:(?:\s*[\[({{]\s*dot\s*[\])}}]\s*|\s+dot\s+|\.){_DOMAIN_LABEL})+",
        re.IGNORECASE,
    ),
    # name at example dot com. A bare "at" needs a "dot" or a real domain after it, so
    # "meet the builder at the lot" is left alone.
    re.compile(
        rf"{_EMAIL_WORD}\s+at\s+{_DOMAIN_LABEL}(?:\s+dot\s+{_DOMAIN_LABEL})+", re.IGNORECASE
    ),
)
# "dana at example.com". It runs after the scheme and www links, so "tour at www.example.com"
# is a link, and before bare domains, which would leave the name behind.
_AT_DOMAIN_EMAIL = re.compile(
    rf"{_EMAIL_WORD}\s+at\s+(?:{_DOMAIN_LABEL}\.)+[a-z]{{2,}}\b", re.IGNORECASE
)

_PHONE = re.compile(
    r"(?<![\w$,.])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
    # A seven-digit number only counts when it follows a cue word, and the cue rule has
    # already taken those clauses; "555-0187" alone is an exchange and a line number.
)

_TOP_LEVEL = r"(?:com|net|org|io|co|us|biz|info|realty|homes|app|dev|xyz|tv|me)"
_LINK_END = r"[^\s]*[^\s.,;:!?)\]]"
_SCHEME_LINKS = re.compile(rf"(?:\bhttps?://|\bwww\.){_LINK_END}", re.IGNORECASE)
_BARE_DOMAINS = re.compile(
    rf"\b{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\.{_TOP_LEVEL}\b(?:/{_LINK_END}|/)?", re.IGNORECASE
)

_NAME_PART = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
# "Dr" is also a street suffix, so a doctor needs the full stop; "Elm Dr Lot 4" is an address.
_HONORIFIC_NAME = re.compile(
    rf"\b(?:(?:Mr|Mrs|Ms|Miss|Mx)\.?|(?:Dr|Prof)\.)\s+{_NAME_PART}(?:\s+{_NAME_PART})?"
)

_BROKERAGE_PREFIX = r"(?:(?:[A-Z][\w'\u2019.-]*|&)\s+){1,4}"
# "Real Estate" and "Properties" are ordinary words too: "Dallas Real Estate taxes" and
# "Properties of the lot" stay unless the name ends in "Group".
_ORDINARY_CONTINUATION = (
    r"tax(?:es)?|market|values?|prices?|records?|laws?|of|in|on|at|are|is|was|were"
    r"|include[sd]?|have|has|can|will"
)
_BROKERAGE = re.compile(
    rf"\b{_BROKERAGE_PREFIX}(?:Realty|Realtors?|Brokerage"
    rf"|(?:Real\s+Estate|Properties)(?:\s+Group\b|(?!\s+(?:{_ORDINARY_CONTINUATION})\b)))"
)

_LICENSE = re.compile(
    r"\b(?:TREC|Licen[sc]e|Lic)\b\.?(?:\s*(?:No\.?|Number|#|:))*\s*\d[\w-]{4,}", re.IGNORECASE
)

# The brokerage rule goes first: "Brokerage" is also a cue word, and the cue rule would take
# the end of "Northpark Brokerage" and leave "Northpark" behind.
_RULES = (
    _BROKERAGE,
    _CUE_CLAUSE,
    *_EMAILS,
    _PHONE,
    _SCHEME_LINKS,
    _AT_DOMAIN_EMAIL,
    _BARE_DOMAINS,
    _HONORIFIC_NAME,
    _LICENSE,
)


@dataclass(frozen=True)
class Redacted:
    text: str
    count: int


def redact_personal(text: str) -> Redacted:
    """`text` with every match of the personal-data rules replaced by `[contact removed]`,
    and how many replacements were made."""
    count = 0
    for rule in _RULES:
        text, replaced = rule.subn(REMOVED, text)
        count += replaced
    return Redacted(text, count)

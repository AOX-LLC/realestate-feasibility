"""Removes personal data from listing remarks at ingestion, before the domain model exists.

The patterns are a heuristic, and they are the only thing standing between a listing agent's
contact details and the database. They catch the forms the synthetic set plants: a cue word
followed by a name or number, emails (including "name at example dot com"), phone numbers,
links, honorific names, brokerage names and licence numbers. A bare first name with no cue
("Maria will meet you there") passes; docs/ARCHITECTURE.md states that gap and the residual
eval set measures it.

Each rule runs on the output of the one before it, in the order of `_RULES` (the plan's
seven, with the brokerage rule first). Redacting already-redacted text changes nothing: the
replacement token is never a cue.
"""

import re
import unicodedata
from dataclasses import dataclass

REMOVED = "[contact removed]"

_CUES = (
    r"call|text|contact|e-?mail|ask\s+for|reach|listed\s+by|listing\s+agent|agent|broker(?:age)?"
    r"|showings?\s+(?:by|with|through)|co-?list(?:ed)?\s+(?:with|by)|presented\s+by"
    r"|courtesy\s+of|represented\s+by|realtor"
    # A label with a colon introduces a contact block ("Showings: Dana Whitfield 214-555-0187").
    r"|(?:showings?|phone|tel)(?=\s*:)"
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
# A cue followed at once by a colon, a dash or a line break ("Listing agent - Dana Whitfield",
# "Agent: Dana", "Call:\nDana") has an empty clause of its own: the separator is part of the
# cue and the clause it introduces follows. A bare full stop is not ("Please call. Thanks!").
_CUE_LEAD = r"(?:[ \t]*(?:[:\u2013\u2014-]|\n)[\s:\u2013\u2014-]*)?"
# The "[" guard keeps the replacement token's own word "contact" from being a cue.
_CUE_CLAUSE = re.compile(rf"(?<!\[)\b(?:{_CUES})\b{_CUE_LEAD}{_CLAUSE_CHAR}*", re.IGNORECASE)

# Every pattern that scans a run of word characters starts with a lookbehind, so a match can
# begin only at the start of a run. Without it a long run with no "@" costs a scan from every
# character in it: quadratic, and one hostile remark could stall the daily sync.
_EMAIL_WORD = r"(?<![\w.+-])[\w.+-]+"
_DOMAIN_LABEL = r"[\w-]+"
_EMAILS = (
    # dana@example.com, dana @ example.com
    re.compile(rf"{_EMAIL_WORD}\s*@\s*{_DOMAIN_LABEL}(?:\s*\.\s*{_DOMAIN_LABEL})*"),
    # A handle: @whitfieldhomes
    re.compile(r"(?<![\w@])@[A-Za-z0-9_][\w.]+"),
    # name [at] example [dot] com, name (at) example (dot) com, name{at}example.com
    re.compile(
        rf"{_EMAIL_WORD}\s*[\[({{]\s*at\s*[\])}}]\s*{_DOMAIN_LABEL}"
        rf"(?:(?:\s*[\[({{]\s*dot\s*[\])}}]\s*|\s+dot\s+|\.){_DOMAIN_LABEL})+",
        re.IGNORECASE,
    ),
    # name at example dot com. A bare "at" needs a "dot" or a real domain after it, so
    # "meet the builder at the lot" is left alone.
    re.compile(
        rf"{_EMAIL_WORD}\s+at\s+{_DOMAIN_LABEL}(?:\s+(?:dot|\.)\s+{_DOMAIN_LABEL})+",
        re.IGNORECASE,
    ),
)
# "dana at example.com". It runs after the scheme and www links, so "tour at www.example.com"
# is a link, and before bare domains, which would leave the name behind.
_AT_DOMAIN_EMAIL = re.compile(
    rf"{_EMAIL_WORD}\s+at\s+(?:{_DOMAIN_LABEL}\.)+[a-z]{{2,}}\b", re.IGNORECASE
)

# Separators between the groups of digits: a space, dot, slash, middle dot, minus sign or any
# of the hyphens and dashes (U+2010 to U+2015, which word processors and PDF copy-paste
# produce), up to five in a row ("214  -  555  -  0187"). A seven-digit number only counts
# after a cue word, and the cue rule has already taken those clauses; "555-0187" alone is an
# exchange and a line number.
_PHONE_SEPARATOR = r"[\s.\-/\u2010-\u2015\u2212\u00b7]{0,5}"
# Not inside a longer number or after a currency sign, but fine after punctuation
# ("Info.214-555-0187", "Lot,214-555-0187").
_PHONE = re.compile(
    rf"(?<![\w$])(?<!\d[,.])(?:\+?1{_PHONE_SEPARATOR})?(?:\(\d{{3}}\)|\d{{3}})"
    rf"{_PHONE_SEPARATOR}\d{{3}}{_PHONE_SEPARATOR}\d{{4}}(?!\d)"
)

_TOP_LEVEL = r"(?:com|net|org|io|co|us|biz|info|realty|homes|app|dev|xyz|tv|me)"
_LINK_END = r"[^\s]*[^\s.,;:!?)\]]"
_SCHEME_LINKS = re.compile(rf"(?:\bhttps?://|\bwww\.){_LINK_END}", re.IGNORECASE)
_BARE_DOMAINS = re.compile(
    rf"(?<![\w.-]){_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\.{_TOP_LEVEL}\b(?:/{_LINK_END}|/)?",
    re.IGNORECASE,
)

# A name: "Alvarez", "Okafor-Reyes", "O'Neil", "O\u2019Neil", "ALVAREZ".
_NAME_PART = r"(?:[A-Z]['\u2019])?[A-Z][A-Za-z]+(?:[-'\u2019][A-Z][A-Za-z]+)?"
# "Dr" is also a street suffix, so a doctor needs the full stop; "Elm Dr Lot 4" is an address.
# The honorific is case-insensitive and the name may be in capitals: "MR. ALVAREZ".
_HONORIFIC_NAME = re.compile(
    rf"\b(?i:(?:Mr|Mrs|Ms|Miss|Mx)\.?|(?:Dr|Prof)\.)\s+{_NAME_PART}(?:\s+{_NAME_PART})?"
)

# Each name word starts only where a word starts: without the lookbehind a run like "Ab-Ab-Ab-"
# is rescanned from every capital in it.
_BROKERAGE_PREFIX = r"(?:(?<![\w'\u2019.&-])(?:[A-Z][\w'\u2019.-]*|&)\s+){1,4}"
# "Real Estate" and "Properties" are ordinary words too: "Dallas Real Estate taxes" and
# "Properties of the lot" stay unless the name ends in "Group".
_ORDINARY_CONTINUATION = (
    r"tax(?:es)?|market|values?|prices?|records?|laws?|of|in|on|at|are|is|was|were"
    r"|include[sd]?|have|has|can|will"
)
_BROKERAGE = re.compile(
    rf"\b{_BROKERAGE_PREFIX}(?i:Realty|Realtors?|Brokerage"
    rf"|(?:Real\s+Estate|Properties)(?:\s+Group\b|(?!\s+(?:{_ORDINARY_CONTINUATION})\b)))"
)

# Whatever the rules above removed, a name left sitting next to it goes too: "Questions? Dana
# Whitfield, 214-555-0187" has no cue word, but the number is gone and the name is its label.
# Two or three capitalised words (a first and a last name), joined by spaces and an optional
# comma or colon. One word is left alone: "Seller", "Mail" and "Plans:" often precede a token.
_NAME_WORD = r"(?:[A-Z]['\u2019])?[A-Z][a-z]{1,20}(?:[-'\u2019][A-Z][a-z]{1,20})?"
_NAME_BEFORE_TOKEN = re.compile(
    rf"(?<![\w'\u2019-]){_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{1,2}}[ \t]*[,:]?[ \t]+"
    rf"(?=\[contact removed\])\[contact removed\]"
)

_LICENSE = re.compile(
    r"\b(?:TREC|Licen[sc]e|Lic)\b\.?(?:\s*(?:No\.?|Number|#|:|is\b))*\s*\d[\w-]{4,}",
    re.IGNORECASE,
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
    _NAME_BEFORE_TOKEN,
)


@dataclass(frozen=True)
class Redacted:
    text: str
    count: int


def redact_personal(text: str) -> Redacted:
    """`text` with every match of the personal-data rules replaced by `[contact removed]`,
    and how many replacements were made.

    The text is folded to NFKC first, so full-width digits and letters cannot hide a number.
    Zero-width characters are the caller's to remove (`llm.untrusted.normalise_untrusted`):
    `sources.mls.reso.ingest_remarks` does both, in that order, and is the only path remarks
    take into a listing."""
    text = unicodedata.normalize("NFKC", text)
    count = 0
    for rule in _RULES:
        text, replaced = rule.subn(REMOVED, text)
        count += replaced
    return Redacted(text, count)

"""The narrative's schema and the check that decides whether a draft may be stored.

Rule zero: code is the authority, model output is evidence. A draft survives only if every
number in it is a figure string the facts sheet gave the model, copied exactly:

* Each allowed figure string is found in each text field, longest first, and masked. An
  occurrence counts only when it stands alone: not touching a letter or digit, not inside a longer
  number (`1,420,799.85` does not contain `420,799.85`), and not signed differently than code
  wrote it (`-7.57%` is not `7.57%`).
* Any digit left over is an `unlisted_figure`: `$107,560` (rounded), `7.6%`, `108k`, `$1.4M`,
  `2026`, `50x150`.
* A spelled quantity left over is a `spelled_number`: two to ninety-nine, hundred, thousand,
  million, billion, dozen, percent, and the fractions (a third, a quarter). "one", "single",
  "half", "double" and "twice" are prose.
* Text outside plain printable ASCII and a few typographic marks is an `odd_character`: it is how
  a number could hide (a zero-width character inside "two", a look-alike letter, a non-ASCII digit).
* Every basis code is one the facts sheet names; lengths are capped; the summary is not blank.

The length caps live here and not in the model's schema, so an overlong draft gets the repair
attempt with feedback instead of failing the whole candidate as a structured error.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from feasibility.llm.facts import Facts

SUMMARY_MAX_CHARS = 700
RISKS_MAX = 5
RISK_TEXT_MAX_CHARS = 300
BASIS_MAX = 3
CHECKS_MAX = 4
CHECK_TEXT_MAX_CHARS = 200
TOKEN_MAX_CHARS = 40
# A rejected result keeps its violations, and each holds a few characters of model text, so how
# many are kept is bounded; whether the draft passed does not depend on the cut.
MAX_VIOLATIONS = 20

ViolationKind = Literal[
    "unlisted_figure", "spelled_number", "unknown_basis", "too_long", "empty", "odd_character"
]


# --- what the model fills ----------------------------------------------------------------------


class RiskPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    basis: list[str] = Field(
        description="One to three codes from the facts sheet that this risk rests on."
    )
    text: str = Field(description="One or two sentences. At most 300 characters.")


class NarrativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(description="A short plain-language summary. At most 700 characters.")
    risks: list[RiskPoint] = Field(description="At most five risk points.")
    checks_before_offer: list[str] = Field(
        description="At most four things to confirm before an offer, each at most 200 characters."
    )


# --- what the check reports -------------------------------------------------------------------


class Violation(BaseModel):
    """What was wrong and where or with what token. Never a sentence of the draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ViolationKind
    text: str


class QuotedFigure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    text: str


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    violations: list[Violation]
    figures_quoted: list[QuotedFigure]


# --- the scan of one text ----------------------------------------------------------------------

# Built from code points: the formatter rewrites \u escapes as literal characters, which the
# ambiguous-character lint then refuses.
_ALLOWED_MARKS = frozenset(
    chr(point) for point in (0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2026)
)
_SIGNS = frozenset("-+<>") | {chr(0x2212), chr(0x2013), chr(0x2014)}
_MASK = chr(0x2588)
_TOKEN_PUNCTUATION = ".,:;-+/"  # noqa: S105 (punctuation that may trail a number, not a secret)
_TOKEN_MARKS = frozenset("$%")
# Number words are a blocklist, so it is matched generously: stems anywhere in a letter run (so
# "twentyfive" and "twenties" fail), short words as whole pieces (so "tenant" passes and "tenfold"
# fails), the words with the hyphens, dots and underscores between letters taken out ("tw-o"),
# runs of single letters joined ("t w o"), and a squashed copy for the longest words ("hun dred").
# A number in a language or slang this list lacks is out of reach of a list; the digit rule, the
# facts sheet and the closed schema are the guards that do not depend on it.
_LONG_STEMS = re.compile(
    r"twent|thirt(?:y|ies|ieth|een)|fort(?:y|ies|ieth)|fift(?:y|ies|ieth|een|h)"
    r"|sixt(?:y|ies|ieth|een)|sevent(?:y|ies|ieth|een)|eight(?:y|ies|ieth|een|h)"
    r"|ninet(?:y|ies|ieth|een)|hundred|thousand|illion|dozen|twelf|eleven|fourteen"
)
_SHORT_WORDS = "zero|two|three|four|five|six|seven|eight|nine|ten|nil|naught|nought"
_SHORT_COMPOUND = re.compile(rf"(?:(?:{_SHORT_WORDS})(?:s|es|th|ths|ish|fold)?)+")
_WHOLE_WORDS = frozenset(
    (
        "couple",
        "pair",
        "trio",
        "score",
        "scores",
        "gross",
        "triple",
        "treble",
        "thrice",
        "quadruple",
        "doz",
        "dozens",
        "grand",
        "grands",
        "mil",
        "mils",
        "bil",
        "mn",
        "mln",
        "bn",
        "bln",
        "mm",
        "tn",
        "thou",
        "lakh",
        "crore",
        "buck",
        "bucks",
        "tenner",
        "fiver",
        "cnote",
        "percent",
        "pct",
        "quarter",
        "quarters",
        "third",
        "thirds",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "tenths",
        "eleventh",
        "twelfth",
        "digit",
        "digits",
        "bp",
        "bps",
        "teen",
        "teens",
        "fourscore",
        "k",
        "g",
        "m",
        "b",
        "t",
        "dos",
        "tres",
        "cuatro",
        "cinco",
        "seis",
        "siete",
        "ocho",
        "nueve",
        "diez",
        "cien",
        "ciento",
        "deux",
        "trois",
        "quatre",
        "cinq",
        "sept",
        "huit",
        "neuf",
        "dix",
        "zwei",
        "drei",
        "vier",
        "funf",
        "sechs",
        "sieben",
        "acht",
        "neun",
        "zehn",
        "hundert",
        "mille",
    )
)
_SQUASHED_STEMS = re.compile(
    r"thousand|hundred|illion|dozen|twenty|thirty|fifty|sixty|seventy|ninety"
)
_PER_CENT = re.compile(r"\bper[\s-]cent\b|\bbasis\s+points?\b", re.IGNORECASE)
_ROMAN = re.compile(r"[MDCLXVI]{2,}")
_LOOKALIKE_NUMBER = re.compile(r"\$?[lIOS]+(?:[.,][lIOS]+)*%?")
_APOSTROPHE_IN_WORD = re.compile(r"(?<=[A-Za-z])[\u2018\u2019'](?=[A-Za-z])")
# Whitespace may follow the mark: a word hyphenated at a line break reads whole.
_JOINED_BY_PUNCTUATION = re.compile(r"(?<=[A-Za-z])[-._]\s*(?=[A-Za-z])")
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z][ \t\n]+){2,}[A-Za-z]\b")
_DIGIT = re.compile(r"[0-9]")
# What may not follow a figure, however far off it sits (whitespace, a hyphen, a bracket): a
# magnitude (`$1,000.00 k`, `mil`), a rate or unit (`/mo`, `per month`, `psf`, `APR`).
_AFTER_FIGURE = re.compile(
    r"[\s\-(^*'\u2018\u2019]*(?:"
    r"(?:k|m|b|g|t|mm|bn|mn|mln|bln|bil|mil|tn|thou|lakh|crore|million|billion|thousand|grand"
    r"|psf|sf|apr|apy|pct|mo|yr|annually|monthly|yearly"
    r"|sq|sqft|square|acres?|months?|years?|comps?)\b"
    r"|/"
    r"|per\s+(?:month|mo|year|yr|annum|sq|sf|square|unit|comp|lot|day|week|acre|foot|ft)\b)",
    re.IGNORECASE,
)
# Words that, just before a figure, change what it says.
_BEFORE_FIGURE = re.compile(
    r"\b(?:minus|negative|neg|plus|cad|usd|eur|gbp|mxn|aud)\s*$", re.IGNORECASE
)


def _is_odd(character: str) -> bool:
    if character in "\n\t" or character in _ALLOWED_MARKS:
        return False
    return not (" " <= character <= "~")


def _stands_alone(text: str, start: int, end: int) -> bool:
    """Whether the span [start, end) is a whole figure and not a piece of something else."""
    if start > 0:
        before = text[start - 1]
        # A sign or a dollar sign in front changes what the figure says: -7.57%, $3,168 sq ft.
        if before.isalnum() or before in _SIGNS or before == "$":
            return False
        if before in ",." and start > 1 and text[start - 2].isdigit():
            return False
        # The same sign with whitespace between: `- $107,560.14`, `minus 7.57%`.
        gap = text[:start].rstrip()
        if gap and (gap[-1] in _SIGNS or _BEFORE_FIGURE.search(gap)):
            return False
    if end < len(text):
        after = text[end]
        # A percent sign after a figure changes its unit: $420,000.00%.
        if after.isalnum() or after == "%":
            return False
        if _AFTER_FIGURE.match(text, end):
            return False
        if after in ",." and end + 1 < len(text) and text[end + 1].isdigit():
            return False
    return True


def _mask_figures(text: str, figures: dict[str, list[str]]) -> tuple[str, list[tuple[str, str]]]:
    """`text` with each standing-alone figure replaced by a mask, and the (key, text) pairs found.

    `figures` maps a figure's text to the keys that carry it. Longest strings go first, so a
    figure is never matched inside a longer one."""
    masked = list(text)
    taken = [False] * len(text)
    found: list[tuple[str, str]] = []
    for figure in sorted(figures, key=lambda f: (-len(f), f)):
        position = text.find(figure)
        while position != -1:
            end = position + len(figure)
            if not any(taken[position:end]) and _stands_alone(text, position, end):
                masked[position:end] = _MASK * len(figure)
                taken[position:end] = [True] * len(figure)
                found.extend((key, figure) for key in figures[figure])
            position = text.find(figure, position + 1)
    return "".join(masked), found


def _numeric_tokens(masked: str) -> list[str]:
    """Each run of word characters and number punctuation that holds a digit, trimmed."""
    tokens: list[str] = []
    run: list[str] = []
    for character in [*masked, " "]:
        if character.isalnum() or character in _TOKEN_MARKS or character in _TOKEN_PUNCTUATION:
            run.append(character)
            continue
        word = "".join(run).rstrip(_TOKEN_PUNCTUATION).lstrip(".,:;/")
        if _DIGIT.search(word) and word not in tokens:
            tokens.append(word[:TOKEN_MAX_CHARS])
        run = []
    return tokens


def _letter_tokens(text: str) -> list[str]:
    """Runs of letters, apostrophes inside a word dropped. A run touching a digit belongs to a
    number (`108k`) that the digit rule already reports."""
    return re.findall(r"(?<![0-9])[A-Za-z]+(?![0-9])", _APOSTROPHE_IN_WORD.sub("", text))


def _is_number_word(token: str) -> bool:
    lowered = token.lower()
    return (
        lowered in _WHOLE_WORDS
        or _LONG_STEMS.search(lowered) is not None
        or _SHORT_COMPOUND.fullmatch(lowered) is not None
    )


def _spelled_numbers(masked: str) -> list[str]:
    """The number words in `masked`, as the tokens they were found in, sorted."""
    found = {token.lower() for token in _letter_tokens(masked) if _is_number_word(token)}
    found |= {token for token in _letter_tokens(masked) if _ROMAN.fullmatch(token)}
    found |= {
        " ".join(re.split(r"[\s-]+", match.group(0).lower()))
        for match in _PER_CENT.finditer(masked)
    }
    # The same words with the separators taken out, so a split spelling is read whole. A token
    # is reported only for what the plain scan did not already name.
    joined = _JOINED_BY_PUNCTUATION.sub("", _SPACED_LETTERS.sub(_without_space, masked))
    for token in _letter_tokens(joined):
        lowered = token.lower()
        if _is_number_word(token) and not any(
            lowered in known or known in lowered for known in found
        ):
            found.add(lowered)
    squashed = "".join(_letter_tokens(masked)).lower()
    for match in _SQUASHED_STEMS.finditer(squashed):
        if not any(match.group(0) in known for known in found):
            found.add(match.group(0))
    return sorted(token[:TOKEN_MAX_CHARS] for token in found)


def _without_space(match: re.Match[str]) -> str:
    return re.sub(r"\s+", "", match.group(0))


def _lookalike_numbers(masked: str) -> list[str]:
    """Digit look-alikes written in ASCII: $lOO,OOO.OO, lO%, O.OO%."""
    found = []
    for token in re.findall(r"[\w$%.,]+", masked):
        word = token.strip(".,")
        punctuated = any(mark in word for mark in "$%,.")
        if punctuated and _LOOKALIKE_NUMBER.fullmatch(word) and re.search(r"[lIO]", word):
            found.append(word[:TOKEN_MAX_CHARS])
    return sorted(set(found))


def _scan_text(
    location: str, text: str, figures: dict[str, list[str]]
) -> tuple[list[Violation], list[tuple[str, str]]]:
    violations: list[Violation] = []
    if any(_is_odd(character) for character in text):
        violations.append(Violation(kind="odd_character", text=location))
    masked, quoted = _mask_figures(text, figures)
    violations += [
        Violation(kind="unlisted_figure", text=token) for token in _numeric_tokens(masked)
    ]
    violations += [
        Violation(kind="unlisted_figure", text=token) for token in _lookalike_numbers(masked)
    ]
    violations += [Violation(kind="spelled_number", text=word) for word in _spelled_numbers(masked)]
    return violations, quoted


# --- the check ---------------------------------------------------------------------------------


def _by_text(figures: dict[str, str]) -> dict[str, list[str]]:
    keys_by_text: dict[str, list[str]] = {}
    for key, text in sorted(figures.items()):
        keys_by_text.setdefault(text, []).append(key)
    return keys_by_text


def _text_fields(draft: NarrativeDraft) -> list[tuple[str, str]]:
    fields = [("summary", draft.summary)]
    fields += [(f"risks[{n}].text", risk.text) for n, risk in enumerate(draft.risks)]
    fields += [
        (f"checks_before_offer[{n}]", item) for n, item in enumerate(draft.checks_before_offer)
    ]
    return fields


def _length_violations(draft: NarrativeDraft) -> list[Violation]:
    found: list[Violation] = []

    def too_long(location: str, size: int, limit: int) -> None:
        if size > limit:
            found.append(Violation(kind="too_long", text=location))

    too_long("summary", len(draft.summary), SUMMARY_MAX_CHARS)
    too_long("risks", len(draft.risks), RISKS_MAX)
    too_long("checks_before_offer", len(draft.checks_before_offer), CHECKS_MAX)
    for number, risk in enumerate(draft.risks):
        too_long(f"risks[{number}].text", len(risk.text), RISK_TEXT_MAX_CHARS)
        too_long(f"risks[{number}].basis", len(risk.basis), BASIS_MAX)
    for number, item in enumerate(draft.checks_before_offer):
        too_long(f"checks_before_offer[{number}]", len(item), CHECK_TEXT_MAX_CHARS)
    return found


def _emptiness_and_basis_violations(draft: NarrativeDraft, facts: Facts) -> list[Violation]:
    found: list[Violation] = []
    for location, text in _text_fields(draft):
        if not text.strip():
            found.append(Violation(kind="empty", text=location))
    allowed = set(facts.codes)
    for number, risk in enumerate(draft.risks):
        if not risk.basis:
            found.append(Violation(kind="unknown_basis", text=f"risks[{number}].basis"))
        found += [
            Violation(kind="unknown_basis", text=code[:TOKEN_MAX_CHARS])
            for code in risk.basis
            if code not in allowed
        ]
    return found


def check_narrative(draft: NarrativeDraft, facts: Facts) -> Check:
    """Whether `draft` may be stored, and every figure in it mapped back to its facts key."""
    figures = _by_text(facts.figures)
    violations = _emptiness_and_basis_violations(draft, facts) + _length_violations(draft)
    quoted: list[tuple[str, str]] = []
    for location, text in _text_fields(draft):
        found_violations, found_figures = _scan_text(location, text, figures)
        violations += found_violations
        quoted += found_figures
    return Check(
        passed=not violations,
        violations=violations[:MAX_VIOLATIONS],
        figures_quoted=[QuotedFigure(key=key, text=text) for key, text in sorted(set(quoted))],
    )

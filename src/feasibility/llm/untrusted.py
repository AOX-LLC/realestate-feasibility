"""Hygiene for text we did not write, before it can reach a prompt.

Listing remarks are untrusted: a seller or agent can write anything into them. This module is
the first of three layers (the others are a closed output schema and code that verifies every
quote). It does three things:

* `normalise_untrusted` (at ingestion, first): Unicode NFKC, and removal of control,
  zero-width, bidi, tag and filler characters, which are counted.
* `finish_untrusted` (at ingestion, last, after redaction): the length cap, so a cut can never
  leave half a phone number, and, when `HIDDEN_TEXT_THRESHOLD` or more characters were
  removed, a visible marker line. The evidence then survives into the stored text and the
  scan below sees it without any state. The marker goes in after redaction: placed earlier
  it would split a number that was hidden among zero-width characters, and the redactor would
  miss it.
* `defang_tags` and `scan_injection` (at prompt build): close-tag lookalikes are neutralised
  and a heuristic list of attack phrasings is matched. A hit marks the remarks suspicious;
  the extraction code then drops any quote that overlaps a hit's span.

The scan is a heuristic and is not the guard: the structural defences are.
"""

import re
import unicodedata
from dataclasses import dataclass

MAX_REMARKS_CHARS = 4_000
HIDDEN_TEXT_THRESHOLD = 3
# No word in the marker may be a redaction cue ("text" would be one).
HIDDEN_TEXT_MARKER = "[invisible characters removed]"

# Characters that render as nothing but are not in a category the loop below catches.
_INVISIBLE_FILLERS = frozenset(
    {0x115F, 0x1160, 0x180E, 0x2800, 0x3164, 0xFFA0}
    | set(range(0xFE00, 0xFE10))  # variation selectors
    | set(range(0xE0100, 0xE01F0))  # variation selectors supplement
)
_KEPT_CONTROLS = frozenset({"\n", "\t"})


@dataclass(frozen=True)
class Normalised:
    text: str
    removed_invisible: int


def _is_invisible(character: str) -> bool:
    if character in _KEPT_CONTROLS:
        return False
    # Cc: controls. Cf: zero-width, bidi, soft hyphen, the tag block. Co and Cn: private use
    # and unassigned, which a renderer shows as nothing or a box.
    return unicodedata.category(character) in {"Cc", "Cf", "Co", "Cn"} or (
        ord(character) in _INVISIBLE_FILLERS
    )


def normalise_untrusted(text: str) -> Normalised:
    """NFKC, line endings as newlines, invisible characters removed and counted."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    kept = [character for character in text if not _is_invisible(character)]
    return Normalised("".join(kept), len(text) - len(kept))


def finish_untrusted(text: str, removed_invisible: int, limit: int = MAX_REMARKS_CHARS) -> str:
    """`text` capped to `limit` characters, with the hidden-text marker on its own last line
    when `removed_invisible` reaches the threshold. The marker counts toward the limit."""
    if removed_invisible < HIDDEN_TEXT_THRESHOLD:
        return text[:limit].rstrip()
    room = limit - len(HIDDEN_TEXT_MARKER) - 1
    return f"{text[:room].rstrip()}\n{HIDDEN_TEXT_MARKER}".lstrip()


_TAG_START = re.compile(r"<(?=[/!?|A-Za-z])")


def defang_tags(text: str) -> str:
    """`<` that starts a tag-like sequence becomes `[`. Same length, so spans stay valid."""
    return _TAG_START.sub("[", text)


@dataclass(frozen=True)
class InjectionHit:
    rule: str
    # Half-open character offsets into the scanned text (the enclosing sentence of the match).
    start: int
    end: int


_FLAGS = re.IGNORECASE
_RULES: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+(?:all|any|the|your|my|previous|"
        r"prior|above|earlier|preceding|these|those|other))*\W+(?:instructions?|prompts?|rules"
        r"|directions?|guidelines|context)\b"
        r"|\bdo\s+not\s+follow\s+(?:the|your|any)\b[^.!?\n]{0,30}\b(?:instructions?|rules)\b",
        _FLAGS,
    ),
    "role_change": re.compile(
        r"\byou\s+are\s+now\b"
        r"|\bpretend\s+(?:to\s+be|you\s+are)\b"
        r"|\bact\s+as\s+(?:an?\s+)?(?:ai|assistant|chatbot|language\s+model|system|different|new)\b"
        r"|\bfrom\s+now\s+on,?\s+you\b"
        r"|\bnew\s+(?:system\s+)?(?:instructions?|role|task)\s*:"
        r"|\byour\s+(?:new\s+)?(?:role|task|job)\s+is\b",
        _FLAGS,
    ),
    # Upper-case turn labels and chat-template tokens. "System: forced air" is a heating
    # description and stays clean.
    "system_marker": re.compile(
        r"\b(?:SYSTEM|ASSISTANT|HUMAN|USER|DEVELOPER)\s*:"
        r"|<\|[^|>\n]{1,30}\|>"
        r"|\[/?INST\]|<<\s*/?SYS\s*>>"
        r"|^#{2,}\s*(?i:instruction|system|response)\b",
        re.MULTILINE,
    ),
    "output_directive": re.compile(
        r"\breport\s+(?:every|all|each)\s+(?:signal|code|category)"
        r"|\bquote\s*:\s*[<\[\"'“]"
        r"|\binjection_suspected\b"
        r"|\b(?:when|before|after)\s+you\s+(?:answer|respond|reply|write|output|report|extract"
        r"|summari[sz]e|list)\b"
        r"|\b(?:in|into|to)\s+(?:your|the)\s+(?:output|answer|response|reply|summary|json)\b"
        r"|\byour\s+(?:output|answer|response|reply)\s+(?:must|should|will|has\s+to)\b"
        r"|\b(?:state|say|write|put|mention|include|insert|add|output)\s+(?:that\s+)?(?:the\s+)?"
        r"(?:margin|profit|roi|return|arv|price|figure|number|percent(?:age)?)\b[^.!?\n]{0,40}\d",
        _FLAGS,
    ),
    "fence_tag": re.compile(
        r"<\s*/?\s*(?:listing_remarks|remarks|system|instructions?|prompt|assistant|user|human"
        r"|tool\w*|function\w*|output|json)\b[^>\n]{0,80}>?"
        r"|```",
        _FLAGS,
    ),
    "hidden_text": re.compile(re.escape(HIDDEN_TEXT_MARKER)),
}
# A fence tag is its own boundary: the text before it is the seller's, the text after is not.
_NO_EXPANSION_BACKWARD = frozenset({"fence_tag"})
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s|$)|\n")


def scan_injection(text: str) -> list[InjectionHit]:
    """Every attack phrasing in `text`, as the rule that matched and the sentence it sits in.

    Scan the text before `defang_tags`: that rewrites the tags the fence rule looks for. The
    offsets are valid for both, since the rewrite keeps the length."""
    boundaries = [match.end() for match in _SENTENCE_BOUNDARY.finditer(text)]
    hits: list[InjectionHit] = []
    for rule, pattern in _RULES.items():
        for match in pattern.finditer(text):
            start = (
                match.start()
                if rule in _NO_EXPANSION_BACKWARD
                else max((b for b in boundaries if b <= match.start()), default=0)
            )
            end = min((b for b in boundaries if b >= match.end()), default=len(text))
            hits.append(InjectionHit(rule, start, end))
    return sorted(hits, key=lambda hit: (hit.start, hit.end, hit.rule))


def overlaps(start: int, end: int, hits: list[InjectionHit]) -> bool:
    """Whether the half-open span [start, end) shares a character with any hit."""
    return any(start < hit.end and hit.start < end for hit in hits)

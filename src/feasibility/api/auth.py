"""Who may call what: two static bearer tokens, compared in constant time, failing closed.

This module is pure: it parses a header and decides. The middleware that enforces it is in
`api/gate.py`. A token that is not configured never matches anything, so with no tokens set
every gated route answers 401; there is no setting that opens the API.
"""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from enum import StrEnum

from feasibility.config import Settings

MAX_AUTHORIZATION_LENGTH = 512
OPEN_PATHS = frozenset({"/livez"})
TRIGGER_PREFIX = "/triggers/"
_TRIGGER_NAME = re.compile(r"/triggers/[a-z][a-z0-9_-]{0,31}")
_BEARER = re.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr] ([^\s]+)")


class Scope(StrEnum):
    READ = "read"
    TRIGGER = "trigger"


class Access(StrEnum):
    """What a request needs before it may run."""

    OPEN = "open"
    READ = "read"
    TRIGGER = "trigger"
    DENY = "deny"


def parse_bearer(header: str | None) -> str | None:
    """The token of `Authorization: Bearer <token>`: the scheme in any case, exactly one space,
    a header of at most 512 characters. Anything else is None."""
    if header is None or len(header) > MAX_AUTHORIZATION_LENGTH:
        return None
    match = _BEARER.fullmatch(header)
    return match.group(1) if match else None


def required_access(method: str, path: str) -> Access:
    """`GET /livez` is open; `POST /triggers/<name>` needs the trigger token; any other GET or
    HEAD needs the read token, whether or not a route exists there (so an unknown path without a
    token is a 401, not a 404 that reveals what exists). Everything else is denied once the
    caller has authenticated."""
    method = method.upper()
    if method in ("GET", "HEAD"):
        return Access.OPEN if path in OPEN_PATHS else Access.READ
    if method == "POST" and _TRIGGER_NAME.fullmatch(path):
        return Access.TRIGGER
    return Access.DENY


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True)
class TokenSet:
    """The sha256 digests of the configured tokens. An unset token is a digest of random bytes
    no request can produce, so it takes the same path through the comparison and matches
    nothing."""

    read: bytes
    trigger: bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> "TokenSet":
        def digest_of(secret: object) -> bytes:
            value = getattr(secret, "get_secret_value", lambda: None)()
            return _digest(value) if value else os.urandom(32)

        return cls(digest_of(settings.api_read_token), digest_of(settings.api_trigger_token))

    def scope_of(self, presented: str) -> Scope | None:
        """The scope a presented token grants. Both digests are always compared, so the work
        does not depend on which one matches, or on how much of a token is right."""
        digest = _digest(presented)
        is_read = hmac.compare_digest(digest, self.read)
        is_trigger = hmac.compare_digest(digest, self.trigger)
        if is_read:
            return Scope.READ
        if is_trigger:
            return Scope.TRIGGER
        return None


def allows(scope: Scope, access: Access) -> bool:
    return (scope is Scope.READ and access is Access.READ) or (
        scope is Scope.TRIGGER and access is Access.TRIGGER
    )

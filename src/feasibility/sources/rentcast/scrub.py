"""Removes personal data from RentCast responses at the transport boundary, before anything
is validated, cached, stored or logged."""

from typing import Any

# Listing agent and office contact details, and property owner names and mailing addresses.
PERSONAL_FIELDS = frozenset({"listingAgent", "listingOffice", "owner"})
# Not personal on their own, but owner-adjacent and unused: never declared or stored.
UNUSED_OWNER_FIELDS = frozenset({"legalDescription", "ownerOccupied"})
INTENTIONALLY_UNDECLARED = PERSONAL_FIELDS | UNUSED_OWNER_FIELDS


def scrub(body: Any) -> Any:
    """A copy of a decoded JSON body with every personal field removed, at any depth."""
    if isinstance(body, dict):
        return {
            key: scrub(value) for key, value in body.items() if key not in INTENTIONALLY_UNDECLARED
        }
    if isinstance(body, list):
        return [scrub(item) for item in body]
    return body

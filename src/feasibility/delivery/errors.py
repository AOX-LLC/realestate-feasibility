"""What can go wrong sending a brief, as errors that carry a short code and nothing else.

A service's own message can hold a workspace name, a page title or a token fragment, so it is never
kept: an error stores its class and a code checked against `^[a-z0-9_]{1,64}$`, which is all that
reaches a log, a job's `last_error` or a delivery row.
"""

import re

_CODE = re.compile(r"[a-z0-9_]{1,64}")


def safe_code(raw: object) -> str:
    """The service's short error code if it looks like one, else `unknown`."""
    return raw if isinstance(raw, str) and _CODE.fullmatch(raw) else "unknown"


class DeliveryError(RuntimeError):
    """Base class; `code` is all anyone may print."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = safe_code(code)

    def __str__(self) -> str:
        return self.code


class DeliveryConfigError(DeliveryError):
    """The credentials, the database or the channel are wrong or missing: a retry cannot fix it."""


class NotionError(DeliveryError):
    """Notion refused or failed a request."""


class SlackError(DeliveryError):
    """Slack refused or failed a request."""


class TransportError(DeliveryError):
    """The request did not complete (a network error or a timeout)."""


class PdfRenderError(DeliveryError):
    """The renderer refused the document (it asked for something outside the package). The same
    document renders the same way again, so a retry cannot fix it."""

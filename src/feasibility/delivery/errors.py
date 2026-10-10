"""What can go wrong sending a brief, as errors that carry a short code and nothing else.

A service's own message can hold a workspace name, a page title or a token fragment, so it is never
kept: an error stores its class and a code checked against `^[a-z0-9_]{1,64}$`, which is all that
reaches a log, a job's `last_error` or a delivery row.
"""

import re
from typing import Any

_CODE = re.compile(r"[a-z0-9_]{1,64}")


def safe_code(raw: object) -> str:
    """The service's short error code if it looks like one, else `unknown`."""
    return raw if isinstance(raw, str) and _CODE.fullmatch(raw) else "unknown"


class DeliveryError(RuntimeError):
    """Base class; `code` is all anyone may print."""

    # The delivery report, set when a delivery ends in one of its own errors.
    report: Any = None

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = safe_code(code)

    def __str__(self) -> str:
        return self.code


class DeliveryBusyError(DeliveryError):
    """Nothing could be decided for now. Code `busy`: another delivery of this run holds the lock,
    and nothing was sent. Code `recent_call`: a call of an earlier delivery left a row too new to
    call unknown; what was delivered before it stays delivered (see `report`). Trying again later
    is right in both cases."""


class DeliveryIncompleteError(DeliveryError):
    """Some items were refused or failed in a way another try may fix. The report says which; a
    retry sends only what is left."""


class DeliveryUnknownOutcomeError(DeliveryError):
    """A Slack item may or may not have been sent. Never retried by itself: a person looks in the
    channel and decides (`brief deliver --resend slack`)."""


class DeliveryConfigError(DeliveryError):
    """The credentials, the database or the channel are wrong or missing: a retry cannot fix it."""


class NotionError(DeliveryError):
    """Notion refused or failed a request."""


class NotionPageGoneError(NotionError):
    """The page a row was last written to is not there (deleted, or moved to the trash)."""


class SlackError(DeliveryError):
    """Slack refused or failed a request."""


class TransportError(DeliveryError):
    """The request did not complete (a network error or a timeout)."""


class PdfRenderError(DeliveryError):
    """The renderer refused the document (it asked for something outside the package). The same
    document renders the same way again, so a retry cannot fix it."""


class OutcomeUnknownError(DeliveryError):
    """A request that creates or posts something failed in a way that may have succeeded (the
    answer was lost, or the service said 5xx). It is not retried by the client; the delivery
    ledger records it as `unknown` and decides."""


class NotionOutcomeUnknownError(NotionError, OutcomeUnknownError):
    pass


class SlackOutcomeUnknownError(SlackError, OutcomeUnknownError):
    pass

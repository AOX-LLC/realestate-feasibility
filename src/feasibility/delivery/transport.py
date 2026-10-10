"""The seam between a service client and the network.

One client per service, and only the transport differs between mock and live (the RentCast
pattern): `Http*Transport` uses httpx with the token in the `Authorization` header and nowhere
else; `Mock*Transport` answers like the service from fixtures and keeps every request in memory.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from feasibility.delivery.errors import DeliveryError, TransportError

RATE_LIMIT_TRIES = 3
SERVER_RETRY_PAUSES = (1.0, 4.0)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedRequest:
    """A request a mock transport saw: what a test (or a dry run) inspects."""

    method: str
    path: str
    json: dict[str, Any] | None
    content_length: int | None = None


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse: ...


class Pacer:
    """Keeps at least `interval` seconds between calls. The clock and the sleep are injectable,
    so a test never waits."""

    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            self._sleep(self._interval - (now - self._last))
        self._last = self._clock()


def retry_after_seconds(headers: dict[str, str], cap: float = 30.0) -> float:
    """The service's `Retry-After`, bounded: a hostile or broken value cannot park a worker."""
    raw = {key.lower(): value for key, value in headers.items()}.get("retry-after", "1")
    try:
        return max(0.0, min(float(raw), cap))
    except ValueError:
        return 1.0


def send_with_retries(
    send: Callable[[], TransportResponse],
    *,
    pacer: Pacer,
    sleep: Callable[[float], None],
    repeatable: bool,
    fail: Callable[[str], DeliveryError],
) -> TransportResponse:
    """One request, with the retries that are safe, for both services.

    A 429 means the service did not process the request, so it is always waited out (up to
    `RATE_LIMIT_TRIES` times). A 5xx or a lost connection may have been processed, so it is
    retried only for a request that is safe to send twice (`repeatable`), never for one that
    creates or posts something. When retries run out, a lost connection raises
    `fail("network_error")` and a 5xx response is returned for the caller to classify."""
    server_pauses = list(SERVER_RETRY_PAUSES) if repeatable else []
    limited = 0
    while True:
        pacer.wait()
        try:
            response = send()
        except TransportError:
            if not server_pauses:
                raise fail("network_error") from None
            sleep(server_pauses.pop(0))
            continue
        if response.status == 429 and limited < RATE_LIMIT_TRIES:
            limited += 1
            sleep(retry_after_seconds(response.headers))
            continue
        if response.status >= 500 and server_pauses:
            sleep(server_pauses.pop(0))
            continue
        return response

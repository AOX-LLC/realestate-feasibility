"""In-process rate limits and the failed-authentication ban.

One uvicorn process serves the API, so this state is the whole state; it resets on a restart and
would not be shared by a second process (a known gap in ARCHITECTURE). Every map is bounded, so a
flood of distinct addresses cannot grow memory without limit.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

Clock = Callable[[], float]

FAILURES_BEFORE_BAN = 20
FAILURE_WINDOW_S = 600
BAN_S = 900
LIVEZ_PER_MINUTE = 60
ANONYMOUS_PER_MINUTE = 60
MAX_KEYS = 10_000


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    retry_after_s: int = 0


class WindowCounter:
    """A fixed-window counter per key. The window starts at a key's first hit and the count
    resets when it ends. Beyond `max_keys` the oldest key is forgotten."""

    def __init__(
        self, limit: int, window_s: float, clock: Clock = time.monotonic, max_keys: int = MAX_KEYS
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._windows)

    def hit(self, key: str) -> Verdict:
        """Count one request. Refused (with the seconds until the window ends) once the count
        would pass the limit; a refused request is not counted."""
        now = self._clock()
        started, count = self._windows.get(key, (now, 0))
        if now - started >= self._window_s:
            started, count = now, 0
        if count >= self._limit:
            self._windows[key] = (started, count)
            self._windows.move_to_end(key)
            return Verdict(False, max(1, int(started + self._window_s - now) + 1))
        self._windows[key] = (started, count + 1)
        self._windows.move_to_end(key)
        while len(self._windows) > self._max_keys:
            self._windows.popitem(last=False)
        return Verdict(True)


class Ban:
    """Bans an address after too many failed authentications in a window."""

    def __init__(
        self,
        failures: int = FAILURES_BEFORE_BAN,
        window_s: float = FAILURE_WINDOW_S,
        ban_s: float = BAN_S,
        clock: Clock = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._failures = WindowCounter(failures, window_s, clock, max_keys)
        self._ban_s = ban_s
        self._clock = clock
        self._max_keys = max_keys
        self._until: OrderedDict[str, float] = OrderedDict()

    def retry_after_s(self, address: str) -> int:
        """Seconds left on a ban, or 0 when the address is not banned."""
        until = self._until.get(address)
        if until is None:
            return 0
        left = until - self._clock()
        if left <= 0:
            del self._until[address]
            return 0
        return int(left) + 1

    def record_failure(self, address: str) -> None:
        """Count a failed authentication; the one past the limit starts the ban."""
        if not self._failures.hit(address).allowed:
            self._until[address] = self._clock() + self._ban_s
            self._until.move_to_end(address)
            while len(self._until) > self._max_keys:
                self._until.popitem(last=False)


class ApiLimits:
    """Everything the gate counts: reads and triggers per scope, `/livez` per address, and the
    ban."""

    def __init__(
        self,
        reads_per_minute: int,
        triggers_per_hour: int,
        clock: Clock = time.monotonic,
    ) -> None:
        self.reads = WindowCounter(reads_per_minute, 60, clock)
        self.triggers = WindowCounter(triggers_per_hour, 3600, clock)
        self.livez = WindowCounter(LIVEZ_PER_MINUTE, 60, clock)
        # Requests that present no credential at all: they guess nothing, so they do not count
        # toward a ban, but they are not free either. And one log line per address a minute.
        self.anonymous = WindowCounter(ANONYMOUS_PER_MINUTE, 60, clock)
        self.failure_log = WindowCounter(1, 60, clock)
        self.ban = Ban(clock=clock)

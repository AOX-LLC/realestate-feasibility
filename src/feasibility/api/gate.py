"""The gate in front of every route: authentication, scope, rate limits, the ban and a body cap.

A pure ASGI middleware, so it runs before routing: an unknown path without a token is a 401 and
a router added later is gated without any code. It answers 401, 403, 405, 413 and 429 itself;
the security headers are added by the middleware outside it. Nothing here logs a header.
"""

import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from feasibility.api.auth import (
    Access,
    Scope,
    TokenSet,
    allows,
    parse_bearer,
    required_access,
)
from feasibility.api.ratelimit import ApiLimits
from feasibility.config import Settings

log = logging.getLogger(__name__)

AsgiScope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[AsgiScope, Receive, Send], Awaitable[None]]

MAX_BODY_BYTES = 4096
MAX_LOGGED_PATH = 200


def client_address(scope: AsgiScope, header_name: str | None) -> str:
    """The socket peer, or the configured header's value when that is a valid address. A header
    anyone can send must not decide whose requests are counted, so it is read only when the
    operator named it (behind a tunnel every peer would otherwise be the tunnel)."""
    if header_name:
        wanted = header_name.lower().encode("latin-1")
        for name, value in scope.get("headers", []):
            if name == wanted:
                try:
                    return str(ipaddress.ip_address(value.decode("latin-1").strip()))
                except ValueError:
                    break
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _header(scope: AsgiScope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


def _all_headers(scope: AsgiScope, name: bytes) -> list[str]:
    return [str(value.decode("latin-1")) for key, value in scope.get("headers", []) if key == name]


def _loggable(path: str) -> str:
    return path[:MAX_LOGGED_PATH].encode("unicode_escape").decode("ascii")


async def _respond(
    send: Send, status: int, detail: str, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _retry_after(seconds: int) -> list[tuple[bytes, bytes]]:
    return [(b"retry-after", str(seconds).encode())]


class ApiGate:
    def __init__(self, app: ASGIApp, settings: Settings, limits: ApiLimits) -> None:
        self._app = app
        self._tokens = TokenSet.from_settings(settings)
        self._limits = limits
        self._ip_header = settings.api_client_ip_header

    async def __call__(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No websocket route exists, and a future one must not be reachable without a token
            # just because the gate only knew about http: refuse the connection.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path = scope["path"]
        address = client_address(scope, self._ip_header)
        access = required_access(scope["method"], path)
        banned_for = self._limits.ban.retry_after_s(address)
        # The open route is exempt from the ban: it has its own limit, and the container's own
        # healthcheck must not be locked out by someone else's guesses (with a client-address
        # header configured, a forged header could otherwise name the healthcheck's address).
        if banned_for and access is not Access.OPEN:
            await _respond(send, 429, "too many requests", _retry_after(banned_for))
            return

        if access is Access.OPEN:
            verdict = self._limits.livez.hit(address)
            if not verdict.allowed:
                await _respond(send, 429, "too many requests", _retry_after(verdict.retry_after_s))
                return
            await self._app(scope, receive, send)
            return

        # Exactly one Authorization header counts: with two, which one a server reads is a
        # choice an attacker could exploit, so two is a failed attempt.
        sent = _all_headers(scope, b"authorization")
        presented = parse_bearer(sent[0]) if len(sent) == 1 else None
        scope_granted = self._tokens.scope_of(presented) if presented is not None else None
        if scope_granted is None:
            # A request that presents nothing guesses nothing: it is refused but not counted
            # toward a ban (on the host every request arrives from one gateway address, so a
            # browser asking for a favicon would otherwise ban the operator's own curl).
            if sent:
                self._limits.ban.record_failure(address)
            log.warning("auth failed from %s on %s", address, _loggable(path))
            await _respond(send, 401, "authentication required", [(b"www-authenticate", b"Bearer")])
            return
        if access is Access.DENY:
            await _respond(send, 405, "method not allowed")
            return
        if not allows(scope_granted, access):
            await _respond(send, 403, "not allowed")
            return

        counter = self._limits.reads if scope_granted is Scope.READ else self._limits.triggers
        verdict = counter.hit(scope_granted.value)
        if not verdict.allowed:
            await _respond(send, 429, "too many requests", _retry_after(verdict.retry_after_s))
            return

        if access is Access.TRIGGER:
            capped = await self._capped_body(scope, receive, send)
            if capped is None:
                return
            receive = capped
        await self._app(scope, receive, send)

    async def _capped_body(self, scope: AsgiScope, receive: Receive, send: Send) -> Receive | None:
        """Refuse a body over the cap before the route sees it. A declared length is checked
        first; otherwise the body is read up to the cap and handed on from memory."""
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_BODY_BYTES:
                    await _respond(send, 413, "request body too large")
                    return None
            except ValueError:
                await _respond(send, 400, "bad content length")
                return None
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return receive
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                await _respond(send, 413, "request body too large")
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay() -> MutableMapping[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return replay

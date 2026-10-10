"""Attack tests on the API gate and the trigger, through hand-built ASGI scopes.

httpx normalises `..` and `//` and adds headers of its own, so a path or header test sent through
it proves little. These tests call the gate directly with the scope an attacker could produce.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from attack_support import DAY_ONE, FREE, execute, rows, run_day
from conftest import empty_database
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from sqlalchemy import Engine
from test_api import (
    READ_BEARER,
    READ_HEADERS,
    TRIGGER_BEARER,
    TRIGGER_HEADERS,
    _settings,
)

from feasibility.api.app import create_app
from feasibility.api.gate import ApiGate
from feasibility.api.ratelimit import ApiLimits
from feasibility.config import Settings
from feasibility.delivery import store
from feasibility.delivery.build import build_brief
from feasibility.snapshot.load import seed

FIX_5A = "a 5a bug the attack review found; the commit that fixes it removes this mark"


class Spy:
    """The app behind the gate: it records that it was reached and answers 200."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def gated(settings: Settings | None = None) -> tuple[ApiGate, Spy]:
    settings = settings or _settings()
    spy = Spy()
    limits = ApiLimits(settings.api_reads_per_minute, settings.api_triggers_per_hour)
    return ApiGate(spy, settings, limits), spy


async def _call(
    gate: ApiGate,
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    peer: str = "10.1.1.1",
    kind: str = "http",
) -> int:
    scope = {
        "type": kind,
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": (peer, 5000),
    }
    sent: list[dict[str, Any]] = []
    chunks = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await gate(scope, receive, send)
    start = next((m for m in sent if m["type"] in ("http.response.start", "websocket.close")), None)
    if start is None:
        return 0
    return int(start.get("status", start.get("code", 0)))


def call(*args: Any, **kwargs: Any) -> int:
    return asyncio.run(_call(*args, **kwargs))


def bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


def test_o1_a_websocket_does_not_reach_the_app_through_the_gate() -> None:
    gate, spy = gated()

    call(gate, "GET", "/parcels", kind="websocket")

    assert spy.reached == []


@pytest.mark.parametrize(
    "path",
    ["//livez", "/livez/..", "/livez/", "/LIVEZ", "/livez\x00", "/%6civez", "/triggers%2Fmorning"],
)
def test_o8_only_exactly_livez_is_open(path: str) -> None:
    gate, spy = gated()

    status = call(gate, "GET", path)

    assert status == 401, path
    assert spy.reached == []


def test_o8_livez_itself_is_open_to_get_and_head() -> None:
    gate, spy = gated()

    assert call(gate, "GET", "/livez") == 200
    assert call(gate, "HEAD", "/livez") == 200
    assert spy.reached == ["/livez", "/livez"]


@pytest.mark.parametrize("method", ["OPTIONS", "TRACE", "PROPFIND", "PUT", "DELETE", "PATCH"])
def test_o9_other_methods_are_401_without_a_token_and_405_with_one(method: str) -> None:
    gate, spy = gated()

    assert call(gate, method, "/parcels") == 401
    assert call(gate, method, "/parcels", headers=bearer(READ_BEARER)) == 405
    assert spy.reached == []


def test_o9_head_needs_the_read_token_and_a_lowercase_method_is_not_a_bypass() -> None:
    gate, _ = gated()

    assert call(gate, "HEAD", "/parcels") == 401
    assert call(gate, "HEAD", "/parcels", headers=bearer(READ_BEARER)) == 200
    assert call(gate, "get", "/parcels", headers=bearer(TRIGGER_BEARER)) == 403
    assert call(gate, "post", "/triggers/morning", headers=bearer(READ_BEARER)) == 403


@pytest.mark.parametrize("declared", [b"-1", b"9999", b"+10", b"5000"])
def test_o10_a_body_over_the_cap_is_a_413_whatever_it_declares(declared: bytes) -> None:
    gate, spy = gated()
    headers = [*bearer(TRIGGER_BEARER), (b"content-length", declared)]

    status = call(gate, "POST", "/triggers/morning", headers=headers, body=b"x" * 5000)

    assert status in (400, 413)
    assert spy.reached == []


def test_o4_two_authorization_headers_are_not_a_way_to_pick_the_better_one() -> None:
    gate, spy = gated()
    headers = [*bearer(READ_BEARER), (b"authorization", b"Bearer nope")]

    status = call(gate, "GET", "/parcels", headers=headers)

    assert status == 401
    assert spy.reached == []


def test_o3_requests_with_no_credentials_do_not_ban_the_address() -> None:
    gate, _ = gated()
    for _ in range(40):
        call(gate, "GET", "/favicon.ico", peer="172.18.0.1")

    assert call(gate, "GET", "/parcels", headers=bearer(READ_BEARER), peer="172.18.0.1") == 200


def test_o3_wrong_tokens_still_ban_the_address() -> None:
    gate, _ = gated()
    for _ in range(25):
        call(gate, "GET", "/parcels", headers=bearer("wrong"), peer="172.18.0.1")

    assert call(gate, "GET", "/parcels", headers=bearer(READ_BEARER), peer="172.18.0.1") == 429


def test_o2_a_spoofed_address_header_cannot_ban_the_healthcheck() -> None:
    settings = _settings().model_copy(update={"api_client_ip_header": "CF-Connecting-IP"})
    gate, _ = gated(settings)
    spoof = [*bearer("wrong"), (b"cf-connecting-ip", b"127.0.0.1")]
    for _ in range(25):
        call(gate, "GET", "/parcels", headers=spoof, peer="10.9.9.9")

    # The container's own healthcheck: peer 127.0.0.1, no header.
    assert call(gate, "GET", "/livez", peer="127.0.0.1") == 200


# --- the trigger and the stored brief -----------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=FIX_5A)
def test_o5_a_date_earlier_than_one_already_queued_is_refused(engine: Engine) -> None:
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        later = client.post(
            "/triggers/morning",
            headers=TRIGGER_HEADERS,
            json={"market": "dallas", "as_of": "2026-10-02"},
        )
        earlier = client.post(
            "/triggers/morning",
            headers=TRIGGER_HEADERS,
            json={"market": "dallas", "as_of": "2026-10-01"},
        )

    assert later.status_code == 202
    assert earlier.status_code == 409


@pytest.fixture
def briefed(migrated_engine: Engine) -> Iterator[Any]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    run = run_day(migrated_engine, _settings(), DAY_ONE, RunModel(**FREE))
    with migrated_engine.begin() as connection:
        store.write_brief(connection, build_brief(connection, run.run_id, _settings().data_mode))
    yield migrated_engine, run
    empty_database(migrated_engine)


@pytest.mark.xfail(strict=True, reason=FIX_5A)
def test_o7_a_stored_brief_that_no_longer_matches_its_hash_is_not_served(briefed: Any) -> None:
    engine, run = briefed
    execute(
        engine,
        "UPDATE brief SET content = jsonb_set(content, '{market}', "
        "'\"<!channel> http://evil.example\"') "
        "WHERE run_id = :r",
        r=run.run_id,
    )

    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        response = client.get(f"/sourcing/runs/{run.run_id}/brief", headers=READ_HEADERS)

    assert response.status_code in (409, 500)
    assert "evil.example" not in response.text


def test_o7_an_untouched_brief_is_served(briefed: Any) -> None:
    engine, run = briefed
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        response = client.get(f"/sourcing/runs/{run.run_id}/brief", headers=READ_HEADERS)

    assert response.status_code == 200
    assert json.loads(response.text)["brief"]["run_id"] == run.run_id
    assert rows(engine, "SELECT count(*) FROM brief")[0][0] == 1

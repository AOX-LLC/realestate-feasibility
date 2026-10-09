"""The gate in front of the API: every route but /livez needs a bearer token, a token grants one
scope, and nothing leaks which routes exist or what a token looked like."""

import hashlib
import hmac
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from test_api import (
    READ_BEARER,
    READ_HEADERS,
    TRIGGER_BEARER,
    TRIGGER_HEADERS,
    _settings,
    with_tokens,
)

from feasibility.api import auth
from feasibility.api.app import SECURITY_HEADERS, create_app
from feasibility.api.auth import Access, Scope, TokenSet, parse_bearer, required_access
from feasibility.config import DataMode, Settings


def _app_client(engine: Engine, settings: Settings | None = None) -> TestClient:
    return TestClient(create_app(settings or _settings(), engine), raise_server_exceptions=False)


@pytest.fixture
def anonymous(engine: Engine) -> Iterator[TestClient]:
    with _app_client(engine) as client:
        yield client


def _get_paths(engine: Engine) -> list[str]:
    app = create_app(_settings(), engine)
    paths = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            paths.append(re.sub(r"\{[^}]+\}", "1", route.path))
    return sorted(paths)


# --- pure parts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Bearer  abc", None),
        ("Bearer abc def", None),
        ("Bearer abc\n", None),
        ("Basic abc", None),
        ("Token abc", None),
        ("Bearerabc", None),
        ("Bearer " + "a" * 10_000, None),
        ("Bearer " + "a" * 505, "a" * 505),
        ("Bearer " + "a" * 506, None),
    ],
)
def test_parse_bearer(header: str | None, token: str | None) -> None:
    assert parse_bearer(header) == token


@pytest.mark.parametrize(
    ("method", "path", "access"),
    [
        ("GET", "/livez", Access.OPEN),
        ("HEAD", "/livez", Access.OPEN),
        ("GET", "/health", Access.READ),
        ("GET", "/anything/at/all", Access.READ),
        ("HEAD", "/parcels", Access.READ),
        ("POST", "/triggers/morning", Access.TRIGGER),
        ("POST", "/triggers/", Access.DENY),
        ("POST", "/triggers/a/b", Access.DENY),
        ("POST", "/triggers/Morning", Access.DENY),
        ("POST", "/parcels", Access.DENY),
        ("PUT", "/triggers/morning", Access.DENY),
        ("DELETE", "/jobs", Access.DENY),
        ("OPTIONS", "/parcels", Access.DENY),
        ("GET", "/livez/", Access.READ),
    ],
)
def test_required_access(method: str, path: str, access: Access) -> None:
    assert required_access(method, path) is access


def test_a_token_set_grants_each_scope_and_nothing_else() -> None:
    tokens = TokenSet.from_settings(_settings())

    assert tokens.scope_of(READ_BEARER) is Scope.READ
    assert tokens.scope_of(TRIGGER_BEARER) is Scope.TRIGGER
    assert tokens.scope_of(READ_BEARER + "x") is None
    assert tokens.scope_of(READ_BEARER[:-1]) is None
    assert tokens.scope_of("") is None


def test_unset_tokens_match_nothing_not_even_an_empty_token() -> None:
    tokens = TokenSet.from_settings(
        Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    )

    for presented in ("", "x", READ_BEARER, TRIGGER_BEARER, "0" * 64):
        assert tokens.scope_of(presented) is None


def test_both_digests_are_compared_on_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    tokens = TokenSet.from_settings(_settings())

    tokens.scope_of(READ_BEARER)  # matches the first one
    tokens.scope_of("nothing")

    assert calls == [(32, 32)] * 4


# --- every route is gated -------------------------------------------------------------------


def test_no_header_is_a_401_with_the_challenge_and_the_security_headers(
    anonymous: TestClient, engine: Engine
) -> None:
    for path in _get_paths(engine):
        if path == "/livez":
            continue
        response = anonymous.get(path)

        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "authentication required"}
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)


def test_the_read_token_opens_every_get_route(engine: Engine) -> None:
    with _app_client(engine) as client:
        for path in _get_paths(engine):
            response = client.get(path, headers=READ_HEADERS)

            assert response.status_code not in (401, 403, 405, 429), path


def test_the_trigger_token_opens_no_get_route(anonymous: TestClient, engine: Engine) -> None:
    for path in _get_paths(engine):
        if path == "/livez":
            continue
        assert anonymous.get(path, headers=TRIGGER_HEADERS).status_code == 403, path


def test_the_read_token_cannot_start_a_trigger(anonymous: TestClient) -> None:
    response = anonymous.post("/triggers/morning", headers=READ_HEADERS, json={})

    assert response.status_code == 403
    assert response.json() == {"detail": "not allowed"}


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "Bearer  " + READ_BEARER,
        "Bearer " + READ_BEARER + "x",
        "Bearer " + READ_BEARER[:-1],
        "Bearer " + "a" * 10_000,
        READ_BEARER,
    ],
)
def test_a_malformed_or_wrong_header_is_a_401(anonymous: TestClient, header: str) -> None:
    assert anonymous.get("/parcels", headers={"Authorization": header}).status_code == 401


def test_the_scheme_is_case_insensitive(anonymous: TestClient) -> None:
    lower = anonymous.get("/parcels", headers={"Authorization": f"bearer {READ_BEARER}"})

    assert lower.status_code == 200


def test_with_no_tokens_configured_every_gated_route_is_a_401_and_livez_answers(
    engine: Engine,
) -> None:
    settings = Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
    with _app_client(engine, settings) as client:
        assert client.get("/livez").status_code == 200
        for path in ("/health", "/parcels", "/jobs"):
            for header in ({}, READ_HEADERS, {"Authorization": "Bearer "}):
                assert client.get(path, headers=header).status_code == 401, path


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_interactive_docs_and_the_schema_are_not_served(
    anonymous: TestClient, path: str
) -> None:
    assert anonymous.get(path).status_code == 401
    assert anonymous.get(path, headers=READ_HEADERS).status_code == 404


def test_an_unknown_path_without_a_token_is_a_401_not_a_404(anonymous: TestClient) -> None:
    assert anonymous.get("/no/such/route").status_code == 401
    assert anonymous.get("/no/such/route", headers=READ_HEADERS).status_code == 404


def test_the_openapi_schema_can_still_be_built_for_the_tests(engine: Engine) -> None:
    schema = create_app(_settings(), engine).openapi()

    assert "/parcels" in schema["paths"]


def test_an_unlisted_method_is_refused_after_authentication(anonymous: TestClient) -> None:
    assert anonymous.post("/parcels", headers=READ_HEADERS).status_code == 405
    assert anonymous.delete("/jobs", headers=READ_HEADERS).status_code == 405
    assert anonymous.post("/parcels").status_code == 401


# --- /livez -----------------------------------------------------------------------------------


def test_livez_is_open_and_says_nothing_but_ok(anonymous: TestClient) -> None:
    response = anonymous.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_livez_is_503_when_the_database_is_unreachable() -> None:
    dead = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    with TestClient(create_app(_settings(), dead), raise_server_exceptions=False) as client:
        response = client.get("/livez")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}


def test_health_is_behind_the_read_token(anonymous: TestClient) -> None:
    assert anonymous.get("/health").status_code == 401
    assert anonymous.get("/health", headers=READ_HEADERS).status_code == 200


# --- tokens never leak --------------------------------------------------------------------------


def test_no_token_appears_in_any_response_or_log(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    with _app_client(engine) as client:
        for header in (
            {},
            READ_HEADERS,
            TRIGGER_HEADERS,
            {"Authorization": f"Bearer {READ_BEARER}x"},
            {"Authorization": f"Bearer {TRIGGER_BEARER[:-2]}"},
        ):
            for path in ("/parcels", "/health", "/no/such", "/livez"):
                response = client.get(path, headers=header)
                bodies.append(response.text + str(dict(response.headers)))
        bodies.append(client.post("/triggers/morning", headers=READ_HEADERS, json={}).text)

    everything = "\n".join(bodies) + "\n".join(r.getMessage() for r in caplog.records)
    assert READ_BEARER not in everything
    assert TRIGGER_BEARER not in everything
    assert hashlib.sha256(READ_BEARER.encode()).hexdigest() not in everything
    assert any("auth failed from" in r.getMessage() for r in caplog.records)


def test_the_failed_attempt_log_names_the_address_and_path_and_no_query(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with _app_client(engine) as client:
        client.get("/parcels?secret=do-not-log", headers={"Authorization": "Bearer nope"})

    messages = [r.getMessage() for r in caplog.records if "auth failed" in r.getMessage()]
    assert len(messages) == 1
    assert "/parcels" in messages[0]
    assert "do-not-log" not in messages[0]
    assert "nope" not in messages[0]


def test_a_path_with_control_characters_is_logged_escaped(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with _app_client(engine) as client:
        client.get("/a%0Ab%0D%1B")

    message = next(r.getMessage() for r in caplog.records if "auth failed" in r.getMessage())
    assert "\n" not in message
    assert "\x1b" not in message


def test_settings_for_the_tests_have_both_tokens() -> None:
    assert (
        with_tokens(
            Settings(_env_file=None, data_mode=DataMode.MOCK)  # type: ignore[call-arg]
        ).api_read_token
        is not None
    )

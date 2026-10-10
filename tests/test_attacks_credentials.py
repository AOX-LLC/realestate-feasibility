"""Attack tests, category (c): a credential is never somewhere it should not be.

The tokens here are built at runtime. `xfail(strict=True)` marks an attack that only a later
session's feature can defeat; that session removes the mark when it makes the test pass.
"""

import json
import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from attack_support import DAY_ONE, FREE, REPO, run_day
from conftest import empty_database
from fastapi.testclient import TestClient
from llm_fakes import RunModel
from pydantic import SecretStr
from sqlalchemy import Engine
from test_api import (
    READ_BEARER,
    READ_HEADERS,
    TRIGGER_BEARER,
    TRIGGER_HEADERS,
    _settings,
)

from feasibility.api.app import create_app
from feasibility.config import DataMode, Settings
from feasibility.logging import configure_logging
from feasibility.snapshot.load import seed

SLACK_SENTINEL = "xoxb-not-a-real-credential"
NOTION_SENTINEL = "ntn_" + "n" * 40
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
API_TOKENS = {"API_READ_TOKEN", "API_TRIGGER_TOKEN"}
DELIVERY_VARIABLES = {
    "DELIVERY_MODE",
    "DELIVERY_TARGETS",
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
}
MODEL_VARIABLES = {"AGENT_CORE_ANTHROPIC_API_KEY", "AGENT_CORE_MODE"}


def environment_of(service: str) -> dict[str, Any]:
    environment = SERVICES.get(service, {}).get("environment", {})
    return dict(environment)


# --- which container holds which credential -----------------------------------------------------


def test_c1_the_api_tokens_are_in_the_api_container_only() -> None:
    assert set(environment_of("api")) >= API_TOKENS
    for service in SERVICES:
        if service != "api":
            assert API_TOKENS.isdisjoint(environment_of(service)), service


def test_c3_the_model_key_is_in_worker_and_migrate_only() -> None:
    for service in SERVICES:
        held = "AGENT_CORE_ANTHROPIC_API_KEY" in environment_of(service)
        assert held == (service in ("worker", "migrate")), service


def test_c5_no_env_file_reaches_a_container() -> None:
    for name, service in SERVICES.items():
        assert "env_file" not in service, name
        for volume in service.get("volumes", []):
            source = str(volume).split(":")[0]
            assert source not in (".", "./", "./.env", ".env"), (name, volume)
    dockerignore = (REPO / ".dockerignore").read_text().split()
    assert ".env" in dockerignore and ".env.*" in dockerignore
    dockerfile = (REPO / "Dockerfile").read_text()
    assert not re.search(r"COPY\s+.*\.env", dockerfile)


def test_c2_delivery_variables_are_in_worker_and_migrate_only() -> None:
    for service in SERVICES:
        held = set(environment_of(service)) >= DELIVERY_VARIABLES
        assert held == (service in ("worker", "migrate")), service
        if service not in ("worker", "migrate"):
            assert DELIVERY_VARIABLES.isdisjoint(environment_of(service)), service


def test_c4_n8n_gets_nothing_from_the_app() -> None:
    n8n = SERVICES["n8n"]
    # n8n's own settings only: the time zone, its N8N_* switches and NODES_EXCLUDE (the node types
    # it may not load). None of this application's variables.
    assert set(n8n.get("environment", {})) <= {"GENERIC_TIMEZONE", "TZ", "NODES_EXCLUDE"} | {
        key for key in n8n.get("environment", {}) if key.startswith("N8N_")
    }
    for variable in API_TOKENS | DELIVERY_VARIABLES | MODEL_VARIABLES | {"DATABASE_URL"}:
        assert variable not in json.dumps(n8n)
    assert "env_file" not in n8n
    assert n8n["ports"] == ["127.0.0.1:4503:5678"]


# --- redaction ----------------------------------------------------------------------------------


def test_c6_secret_values_holds_every_configured_secret_whatever_the_mode() -> None:
    kept = "rentcast-" + "k" * 20
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=DataMode.MOCK,
        rentcast_api_key=SecretStr(kept),
        api_read_token=READ_BEARER,
        api_trigger_token=TRIGGER_BEARER,
    )

    values = settings.secret_values()

    # Mock mode ignores the RentCast key, but compose still puts it in the container's
    # environment, so a log line that leaked it must still be redacted.
    assert kept in values
    assert READ_BEARER in values and TRIGGER_BEARER in values


def test_c9_the_real_log_handler_redacts_a_token_in_a_url_and_a_header(
    engine: Engine, capfd: pytest.CaptureFixture[str]
) -> None:
    settings = _settings()
    configure_logging(settings.secret_values())
    try:
        with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client:
            client.get(f"/x/{READ_BEARER}")
            client.get("/parcels", headers={"Authorization": f"Bearer {TRIGGER_BEARER}x"})
            client.get(f"/parcels?probe={TRIGGER_BEARER}", headers=READ_HEADERS)
        logging.getLogger("feasibility.anything").error(
            "leaked %s and %s", READ_BEARER, TRIGGER_BEARER
        )
        logging.getLogger("feasibility.anything").error(
            "the header was %s", f"Bearer {READ_BEARER}"
        )
    finally:
        logging.getLogger().handlers = []
    captured = capfd.readouterr()
    for token in (READ_BEARER, TRIGGER_BEARER):
        assert token not in captured.err and token not in captured.out
    sys.stderr.flush()


# --- what a model sees and what is recorded -----------------------------------------------------


def test_c7_no_credential_is_in_a_recording() -> None:
    patterns = [
        "xoxb-",
        "ntn_",
        "secret_",
        "Bearer ",
        "sk-ant",
        "authorization",
        "x-api-key",
        READ_BEARER,
        TRIGGER_BEARER,
    ]
    files = list((REPO / "data" / "llm" / "replays").rglob("*.json"))
    assert len(files) == 73
    for path in files:
        text = path.read_text(encoding="utf-8").casefold()
        assert not [p for p in patterns if p.casefold() in text], path.name


def test_c7_no_credential_is_in_a_prompt(migrated_engine: Engine) -> None:
    empty_database(migrated_engine)
    settings = _settings().model_copy(
        update={
            "rentcast_api_key": SecretStr("rentcast-" + "k" * 20),
            "llm_api_key": SecretStr("model-" + "m" * 20),
        }
    )
    seed(migrated_engine, settings)
    model = RunModel(**FREE)
    try:
        run_day(migrated_engine, settings, DAY_ONE, model)
        sent = repr(model.calls)
    finally:
        empty_database(migrated_engine)
    assert model.calls
    for secret in (READ_BEARER, TRIGGER_BEARER, "rentcast-" + "k" * 20, "model-" + "m" * 20):
        assert secret not in sent


# --- API responses ------------------------------------------------------------------------------


def test_c8_no_token_is_in_any_api_response(engine: Engine) -> None:
    bodies: list[str] = []
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:

        def keep(response: Any) -> None:
            bodies.append(response.text + str(dict(response.headers)))

        keep(client.get("/health", headers=READ_HEADERS))
        keep(client.get("/jobs", headers=READ_HEADERS))
        keep(client.get("/sourcing/runs/1/brief", headers=READ_HEADERS))
        keep(client.get(f"/parcels?limit={READ_BEARER}", headers=READ_HEADERS))
        keep(client.get(f"/nothing/{READ_BEARER}", headers=READ_HEADERS))
        keep(client.post("/parcels", headers=READ_HEADERS))
        keep(client.post("/triggers/morning", headers=READ_HEADERS, json={}))
        keep(client.post("/triggers/morning", headers=TRIGGER_HEADERS, json={READ_BEARER: 1}))
        keep(
            client.post(
                "/triggers/morning",
                headers=TRIGGER_HEADERS,
                json={"market": "dallas", "as_of": TRIGGER_BEARER},
            )
        )
        keep(
            client.post(
                "/triggers/morning",
                headers=TRIGGER_HEADERS,
                content=b"x" * 9000,
            )
        )
        for _ in range(130):
            response = client.get("/jobs", headers=READ_HEADERS)
        keep(response)
    bodies.append(str(create_app(_settings(), engine).openapi()))

    everything = "\n".join(bodies)
    assert READ_BEARER not in everything
    assert TRIGGER_BEARER not in everything


# --- the things 5b and 5c build -----------------------------------------------------------------


def test_c10_a_configuration_error_names_the_variable_and_never_the_value() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            delivery_mode="live",
            slack_bot_token=SecretStr("not-" + SLACK_SENTINEL),
        )
    assert "SLACK_BOT_TOKEN" in str(raised.value)
    assert SLACK_SENTINEL not in str(raised.value)


def test_c11_the_http_transports_keep_the_token_in_the_header_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import httpx

    from feasibility.delivery.notion import HttpNotionTransport
    from feasibility.delivery.slack import HttpSlackTransport

    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "results": []})

    notion = HttpNotionTransport(
        NOTION_SENTINEL,
        client=httpx.Client(
            base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
        ),
    )
    slack = HttpSlackTransport(
        SLACK_SENTINEL,
        client=httpx.Client(
            base_url="https://slack.com/api", transport=httpx.MockTransport(handler)
        ),
    )
    notion.request("POST", "/v1/pages", json={"properties": {}})
    slack.request("POST", "/chat.postMessage", json={"text": "t"})

    assert len(seen) == 2
    for request, token in zip(seen, (NOTION_SENTINEL, SLACK_SENTINEL), strict=True):
        assert request.headers["authorization"] == f"Bearer {token}"
        assert token not in str(request.url) and token not in request.content.decode()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert NOTION_SENTINEL not in logged and SLACK_SENTINEL not in logged


@pytest.fixture(scope="module")
def two_days(migrated_engine: Engine) -> Iterator[Any]:
    from delivery_harness import run_two_days

    one, two = run_two_days(migrated_engine)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


def live_settings() -> Settings:
    from delivery_harness import settings

    from feasibility.config import DeliveryMode

    return settings(
        delivery_mode=DeliveryMode.LIVE,
        notion_token=SecretStr(NOTION_SENTINEL),
        notion_database_id="0" * 32,
        slack_bot_token=SecretStr(SLACK_SENTINEL),
        slack_channel_id="C0MOCKCHAN",
    )


def http_clients(handler: Any) -> Any:
    """The real HTTP transports, holding the sentinel tokens, over a fake network."""
    import httpx
    from delivery_harness import notion_client, slack_client

    from feasibility.delivery.deliver import Clients
    from feasibility.delivery.notion import BASE_URL as NOTION_URL
    from feasibility.delivery.notion import HttpNotionTransport
    from feasibility.delivery.slack import BASE_URL as SLACK_URL
    from feasibility.delivery.slack import HttpSlackTransport

    network = httpx.MockTransport(handler)
    return Clients(
        notion=notion_client(
            HttpNotionTransport(
                NOTION_SENTINEL, client=httpx.Client(base_url=NOTION_URL, transport=network)
            )
        ),
        slack=slack_client(
            HttpSlackTransport(
                SLACK_SENTINEL,
                client=httpx.Client(base_url=SLACK_URL, transport=network),
                upload_client=httpx.Client(transport=network),
            )
        ),
    )


@pytest.mark.parametrize("digest_posts", [False, True])
def test_c12_failing_transports_leave_no_secret_or_upload_url_in_a_stored_error(
    two_days: Any, caplog: pytest.LogCaptureFixture, digest_posts: bool
) -> None:
    """Every failure says what it can (the token, the upload URL, a workspace's words). In one
    run the digest is refused, so nothing is uploaded; in the other it posts and each upload's
    connection fails with the URL in the exception text."""
    import httpx
    from delivery_harness import clear_ledger, ledger

    from feasibility.delivery.deliver import deliver_brief
    from feasibility.delivery.errors import DeliveryError
    from feasibility.logging import describe_error

    engine, one, _ = two_days
    upload = "https://files.slack.com/upload/v1/" + "u" * 24
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.host or "")
        echo = {"error": f"{SLACK_SENTINEL} {NOTION_SENTINEL} {upload}", "message": SLACK_SENTINEL}
        if request.url.host == "files.slack.com":
            raise httpx.ConnectError(f"cannot reach {request.url} with {SLACK_SENTINEL}")
        if request.url.host == "api.notion.com":
            return httpx.Response(500, json=echo)
        if request.url.path == "/api/chat.postMessage" and digest_posts:
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"})
        if request.url.path == "/api/files.getUploadURLExternal":
            return httpx.Response(200, json={"ok": True, "upload_url": upload, "file_id": "F1"})
        return httpx.Response(200, json={"ok": False, "error": SLACK_SENTINEL})

    caplog.set_level(logging.DEBUG)
    clear_ledger(engine)

    with pytest.raises(DeliveryError) as raised:
        deliver_brief(engine, live_settings(), one.run_id, clients=http_clients(handler))

    printed = describe_error(raised.value)
    assert not [t for t in (SLACK_SENTINEL, NOTION_SENTINEL, "files.slack.com") if t in printed]
    assert ("files.slack.com" in reached) == digest_posts  # the upload path really ran
    kept = json.dumps([dict(r, created_at=None, updated_at=None) for r in ledger(engine)])
    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in (SLACK_SENTINEL, NOTION_SENTINEL, upload, "files.slack.com", "u" * 24):
        assert secret not in kept, secret
        assert secret not in logged, secret
    codes = {r["error_code"] for r in ledger(engine)}
    assert codes <= {None, "http_500", "unknown", "network_error"}
    assert codes - {None}  # something did fail, and said only its code


def test_c13_no_token_is_in_any_payload() -> None:
    import zlib

    from delivery_support import sample

    from feasibility.delivery.document import proforma_document
    from feasibility.delivery.notion import row_properties
    from feasibility.delivery.pdf import render_pdf
    from feasibility.delivery.slack import digest_blocks

    brief, entries = sample()
    tokens = (READ_BEARER, TRIGGER_BEARER, SLACK_SENTINEL, NOTION_SENTINEL)
    payloads = [brief.model_dump_json()]
    for entry, result in entries.values():
        payloads.append(json.dumps(row_properties(brief, entry)))
        document = proforma_document(brief, entry, result)
        payloads.append(document.model_dump_json())
        pdf = render_pdf(document)
        payloads.append(pdf.decode("latin-1"))
        # The streams are compressed: look inside them too.
        for chunk in pdf.split(b"stream\r\n")[1:] + pdf.split(b"stream\n")[1:]:
            try:
                payloads.append(zlib.decompress(chunk.split(b"endstream")[0]).decode("latin-1"))
            except zlib.error:
                continue
    blocks, text = digest_blocks(brief)
    payloads += [json.dumps(blocks), text]

    for payload in payloads:
        assert not [t for t in tokens if t in payload]
        assert "Authorization" not in payload and "Bearer " not in payload


def test_c13_no_token_is_in_any_request_of_a_real_delivery(two_days: Any) -> None:
    """The check above feeds the payload builders; this one runs a delivery over the real HTTP
    transports, which hold the tokens, and reads every request they put on the wire."""
    import json as json_module
    import zlib

    import httpx
    from delivery_harness import clear_ledger

    from feasibility.delivery.deliver import deliver_brief
    from feasibility.delivery.notion import MockNotionTransport
    from feasibility.delivery.slack import MockSlackTransport

    engine, one, _ = two_days
    notion_service, slack_service = MockNotionTransport(), MockSlackTransport()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json_module.loads(request.content) if request.content[:1] == b"{" else None
        if request.url.host == "api.notion.com":
            answer = notion_service.request(request.method, request.url.path, json=body)
        elif request.url.host == "files.slack.com":
            return httpx.Response(slack_service.upload(str(request.url), request.content).status)
        else:
            form = None if body else dict(httpx.QueryParams(request.content.decode()))
            path = request.url.path.removeprefix("/api")
            answer = slack_service.request(request.method, path, json=body, data=form)
        return httpx.Response(answer.status, json=answer.body)

    clear_ledger(engine)

    deliver_brief(engine, live_settings(), one.run_id, clients=http_clients(handler))

    assert len(seen) == 27  # 11 for Notion, and a digest with five files for Slack
    for request in seen:
        host = request.url.host
        wanted = {"api.notion.com": NOTION_SENTINEL, "slack.com": SLACK_SENTINEL}.get(host)
        assert request.headers.get("authorization") == (f"Bearer {wanted}" if wanted else None)
        wire = [str(request.url), request.content.decode("latin-1")]
        for chunk in request.content.split(b"stream\n")[1:]:
            try:
                wire.append(zlib.decompress(chunk.split(b"endstream")[0]).decode("latin-1"))
            except zlib.error:
                continue
        for sent in wire:
            assert SLACK_SENTINEL not in sent and NOTION_SENTINEL not in sent
            assert "Bearer" not in sent and READ_BEARER not in sent and TRIGGER_BEARER not in sent
    # The upload URL is only ever requested over TLS at Slack's upload host, with no credentials.
    uploads = [r for r in seen if r.url.host == "files.slack.com"]
    assert len(uploads) == 5 and {r.url.scheme for r in uploads} == {"https"}


def test_c14_the_n8n_workflow_holds_no_secret() -> None:
    text = (REPO / "n8n" / "morning-brief.json").read_text()
    assert not re.search(r"Bearer [A-Za-z0-9]|xoxb-|ntn_|secret_", text)


def test_c15_tests_never_reach_the_live_services_even_with_tokens_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from feasibility.delivery.notion import MockNotionTransport, build_notion_transport
    from feasibility.delivery.slack import MockSlackTransport, build_slack_transport

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_SENTINEL)
    monkeypatch.setenv("NOTION_TOKEN", NOTION_SENTINEL)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    # The process environment is read, but delivery is mock unless a test says otherwise, so the
    # transports are the in-memory ones and the tokens are dropped.
    assert isinstance(build_slack_transport(settings), MockSlackTransport)
    assert isinstance(build_notion_transport(settings), MockNotionTransport)
    assert settings.slack_bot_token is None and settings.notion_token is None


def test_c15_the_test_session_starts_with_no_delivery_or_service_token_in_its_environment() -> None:
    import os

    held = [
        name
        for name in (
            "DELIVERY_MODE",
            "NOTION_TOKEN",
            "NOTION_DATABASE_ID",
            "SLACK_BOT_TOKEN",
            "SLACK_CHANNEL_ID",
        )
        if name in os.environ
    ]

    assert held == []


def test_c16_ci_masks_the_tokens_it_makes_and_never_traces_a_command() -> None:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text()

    assert "::add-mask::$read_token" in text and "::add-mask::$trigger_token" in text
    assert not re.search(r"curl[^\n]*\s-v\b|set -x|--trace|-vv", text)


@pytest.fixture
def _quiet() -> Iterator[Path]:
    yield REPO

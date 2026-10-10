"""Attack tests, category (c): a credential is never somewhere it should not be.

The tokens here are built at runtime. `xfail(strict=True)` marks an attack that only a later
session's feature can defeat; that session removes the mark when it makes the test pass.
"""

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

LATER_5B = "5b builds this; the session removes this mark when it makes the test pass"
LATER_5C = "5c builds this; the session removes this mark when it makes the test pass"
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


FIX_5A = "a 5a bug the attack review found; the commit that fixes it removes this mark"


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


@pytest.mark.xfail(strict=True, reason=LATER_5B)
def test_c2_delivery_variables_are_in_worker_and_migrate_only() -> None:
    for service in SERVICES:
        held = set(environment_of(service)) >= DELIVERY_VARIABLES
        assert held == (service in ("worker", "migrate")), service
        if service not in ("worker", "migrate"):
            assert DELIVERY_VARIABLES.isdisjoint(environment_of(service)), service


@pytest.mark.xfail(strict=True, reason=LATER_5C)
def test_c4_n8n_gets_nothing_from_the_app() -> None:
    n8n = SERVICES["n8n"]
    assert set(n8n.get("environment", {})) <= {"GENERIC_TIMEZONE", "TZ"} | {
        key for key in n8n.get("environment", {}) if key.startswith("N8N_")
    }
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


@pytest.mark.xfail(strict=True, reason=LATER_5B)
def test_c10_a_configuration_error_names_the_variable_and_never_the_value() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            delivery_mode="live",
            slack_bot_token=SecretStr("not-" + SLACK_SENTINEL),
        )
    assert "SLACK_BOT_TOKEN" in str(raised.value)
    assert SLACK_SENTINEL not in str(raised.value)


@pytest.mark.xfail(strict=True, reason=LATER_5B)
def test_c11_the_http_transports_keep_the_token_in_the_header_only() -> None:
    from feasibility.delivery.notion import HttpNotionTransport  # noqa: F401
    from feasibility.delivery.slack import HttpSlackTransport  # noqa: F401

    raise AssertionError("5b writes this against httpx.MockTransport")


@pytest.mark.xfail(strict=True, reason=LATER_5C)
def test_c12_a_failing_transport_leaves_no_secret_or_upload_url_in_a_stored_error() -> None:
    from feasibility.delivery.deliver import deliver_brief  # noqa: F401

    raise AssertionError("5c writes this against the delivery ledger and describe_error")


@pytest.mark.xfail(strict=True, reason=LATER_5B)
def test_c13_no_token_is_in_any_payload() -> None:
    from feasibility.delivery.slack import digest_blocks  # noqa: F401

    raise AssertionError("5b writes this over the PDF, Slack and Notion payloads")


@pytest.mark.xfail(strict=True, reason=LATER_5C)
def test_c14_the_n8n_workflow_holds_no_secret() -> None:
    text = (REPO / "n8n" / "morning-brief.json").read_text()
    assert not re.search(r"Bearer [A-Za-z0-9]|xoxb-|ntn_|secret_", text)


@pytest.mark.xfail(strict=True, reason=LATER_5B)
def test_c15_tests_never_reach_the_live_services_even_with_tokens_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "live")
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_SENTINEL)
    from feasibility.delivery.slack import build_transport  # noqa: F401

    raise AssertionError("5b writes this: mock transports in tests, sockets blocked")


def test_c16_ci_masks_the_tokens_it_makes_and_never_traces_a_command() -> None:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text()

    assert "::add-mask::$read_token" in text and "::add-mask::$trigger_token" in text
    assert not re.search(r"curl[^\n]*\s-v\b|set -x|--trace|-vv", text)


@pytest.fixture
def _quiet() -> Iterator[Path]:
    yield REPO

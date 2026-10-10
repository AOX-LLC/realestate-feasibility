"""The compose file's resource limits: a container without a memory limit can take the whole
node, which other sessions share."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_FILE = Path(__file__).parent.parent / "docker-compose.yml"
MEMORY_LIMIT = re.compile(r"^[1-9]\d*[mg]$", re.IGNORECASE)


def _services() -> dict[str, dict[str, Any]]:
    # safe_load resolves the anchors and `<<` merges the file uses, so a limit set on a shared
    # anchor counts for the services that merge it.
    services: dict[str, dict[str, Any]] = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    return services


def test_the_compose_file_defines_four_services_and_the_scheduler_as_an_opt_in_profile() -> None:
    services = _services()

    assert sorted(services) == ["api", "db", "migrate", "n8n", "worker"]
    # A plain `docker compose up` (and CI) starts the four; n8n only with --profile schedule.
    assert [name for name, s in services.items() if s.get("profiles")] == ["n8n"]
    assert services["n8n"]["profiles"] == ["schedule"]


@pytest.mark.parametrize("service", sorted(_services()))
def test_every_service_sets_a_memory_limit(service: str) -> None:
    limit = _services()[service].get("mem_limit")

    assert isinstance(limit, str) and MEMORY_LIMIT.match(limit), (
        f"service {service!r} needs a mem_limit such as 256m, got {limit!r}"
    )


LLM_VARIABLES = {
    "AGENT_CORE_MODE",
    "AGENT_CORE_ANTHROPIC_API_KEY",
    "AGENT_CORE_CONFIG",
    "LLM_RUN_BUDGET_USD",
    "LLM_MONTHLY_BUDGET_USD",
}


@pytest.mark.parametrize("service", ["worker", "migrate"])
def test_the_services_that_call_models_get_the_model_settings(service: str) -> None:
    environment = _services()[service]["environment"]

    assert set(environment) >= LLM_VARIABLES
    assert "DATABASE_URL" in environment


API_LLM_VARIABLES = {"LLM_RUN_BUDGET_USD", "LLM_MONTHLY_BUDGET_USD"}


def test_the_api_gets_the_spend_caps_it_reports_but_no_mode_and_no_key() -> None:
    environment = _services()["api"]["environment"]

    assert set(environment) >= API_LLM_VARIABLES
    assert (LLM_VARIABLES - API_LLM_VARIABLES).isdisjoint(environment)
    assert not any("ANTHROPIC" in name for name in environment)
    assert "DATABASE_URL" in environment


def test_the_api_and_the_worker_read_the_same_caps() -> None:
    services = _services()

    for name in API_LLM_VARIABLES:
        assert services["api"]["environment"][name] == services["worker"]["environment"][name]


API_TOKENS = {"API_READ_TOKEN", "API_TRIGGER_TOKEN"}


def test_the_api_alone_gets_the_two_bearer_tokens() -> None:
    services = _services()

    assert set(services["api"]["environment"]) >= API_TOKENS
    for name in ("worker", "migrate", "db"):
        environment = services[name].get("environment", {})
        assert API_TOKENS.isdisjoint(environment), name
        assert "API_CLIENT_IP_HEADER" not in environment, name


def test_the_api_healthcheck_uses_the_open_route() -> None:
    check = " ".join(_services()["api"]["healthcheck"]["test"])

    assert "/livez" in check
    assert "/health'" not in check


def test_the_database_name_comes_from_one_variable_with_a_default() -> None:
    for name in ("api", "worker", "migrate"):
        assert _services()[name]["environment"]["DATABASE_URL"].endswith(
            "${FEASIBILITY_DB:-feasibility}"
        ), name


DELIVERY_VARIABLES = {
    "DELIVERY_MODE",
    "DELIVERY_TARGETS",
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
}


@pytest.mark.parametrize("service", ["worker", "migrate"])
def test_the_services_that_deliver_get_the_delivery_settings(service: str) -> None:
    assert set(_services()[service]["environment"]) >= DELIVERY_VARIABLES


@pytest.mark.parametrize("service", ["api", "db"])
def test_the_api_and_the_database_get_no_delivery_setting(service: str) -> None:
    environment = _services()[service].get("environment", {})

    assert DELIVERY_VARIABLES.isdisjoint(environment)


def test_only_the_worker_and_migrate_get_the_media_folder() -> None:
    services = _services()

    for name in ("worker", "migrate"):
        assert "/media" in " ".join(services[name]["volumes"])
        assert "MEDIA_OUT" in services[name]["environment"]
    for name in ("api", "db", "n8n"):
        assert "/media" not in " ".join(services[name].get("volumes", []))
        assert "MEDIA_OUT" not in services[name].get("environment", {})


RETENTION_VARIABLES = {
    "RETENTION_RUN_DAYS",
    "RETENTION_ESTIMATE_DAYS",
    "RETENTION_MODEL_CACHE_DAYS",
    "RETENTION_LEDGER_MONTHS",
    "RETENTION_JOB_DAYS",
    "RETENTION_LISTING_DAYS",
}


def test_only_the_worker_and_migrate_get_the_retention_settings() -> None:
    services = _services()

    for name in ("worker", "migrate"):
        assert set(services[name]["environment"]) >= RETENTION_VARIABLES
    for name in ("api", "db", "n8n"):
        assert RETENTION_VARIABLES.isdisjoint(services[name].get("environment", {}))

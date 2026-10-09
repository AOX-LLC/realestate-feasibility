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


def test_the_compose_file_defines_the_four_services() -> None:
    assert sorted(_services()) == ["api", "db", "migrate", "worker"]


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


def test_the_api_gets_no_model_settings_and_no_key() -> None:
    environment = _services()["api"]["environment"]

    assert LLM_VARIABLES.isdisjoint(environment)
    assert not any("ANTHROPIC" in name for name in environment)
    assert "DATABASE_URL" in environment

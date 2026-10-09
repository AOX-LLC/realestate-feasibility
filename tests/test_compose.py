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

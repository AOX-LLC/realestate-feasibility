"""THIRD_PARTY_NOTICES.md accounts for every package in uv.lock, and the README links to it."""

import re
import tomllib

from feasibility.config import REPO_ROOT

NOTICES = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
PROJECT = "realestate-feasibility"


def _locked() -> dict[str, str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if p["name"] != PROJECT}


def _runtime() -> set[str]:
    """The packages the project needs to run: the closure of its dependencies in uv.lock,
    including the extras it asks for (`psycopg[binary]`)."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    stack = list(packages[PROJECT].get("dependencies", []))
    seen: set[str] = set()
    while stack:
        found = stack.pop()
        for extra in found.get("extra", []):
            stack.extend(packages[found["name"]].get("optional-dependencies", {}).get(extra, []))
        if found["name"] not in seen:
            seen.add(found["name"])
            stack.extend(packages[found["name"]].get("dependencies", []))
    return seen


def _listed(section: str) -> dict[str, str]:
    """Package -> version from the rows of one of the two tables."""
    heading = "Runtime dependencies" if section == "runtime" else "Development and test"
    body = NOTICES.split(heading, 1)[1]
    if section == "runtime":
        body = body.split("Development and test", 1)[0]
    else:
        body = body.split("### What the licences ask", 1)[0]
    rows = re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| ([0-9][^ |]*) \|", body, re.MULTILINE)
    return dict(rows)


def test_every_locked_package_is_listed_with_its_locked_version() -> None:
    assert {**_listed("runtime"), **_listed("dev")} == _locked()


def test_a_package_is_in_the_runtime_table_exactly_when_the_image_needs_it() -> None:
    assert set(_listed("runtime")) == _runtime()
    assert set(_listed("dev")) == set(_locked()) - _runtime()


def test_the_data_sources_are_credited() -> None:
    assert "Dallas Central Appraisal District" in NOTICES
    assert "Only the layout is used" in " ".join(NOTICES.split())
    assert "never committed" in NOTICES
    assert "RentCast" in NOTICES
    assert "agent-core" in NOTICES


def test_the_readme_links_to_the_notices_from_its_license_section() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    license_section = readme.split("## License", 1)[1]

    assert "(THIRD_PARTY_NOTICES.md)" in license_section


def test_the_services_delivery_uses_and_the_scheduler_image_are_credited() -> None:
    flat = " ".join(NOTICES.split())

    assert "Notion" in NOTICES and "Slack" in NOTICES
    assert "mock mode nothing is sent and no token is read" in flat
    assert "n8n" in NOTICES and "Sustainable Use License" in NOTICES
    # The pinned image in the notices is the one the compose file pulls.
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    version = re.search(r"n8nio/n8n:([0-9.]+)@sha256", compose)
    assert version is not None and f"n8nio/n8n:{version.group(1)}" in NOTICES

"""THIRD_PARTY_NOTICES.md accounts for every package in uv.lock, and the README links to it."""

import re
import tomllib

from feasibility.config import REPO_ROOT

NOTICES = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
PROJECT = "realestate-feasibility"


def _locked() -> dict[str, str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if p["name"] != PROJECT}


def _listed() -> dict[str, str]:
    """Package -> version from the two tables' rows."""
    rows = re.findall(r"^\| ([a-z0-9][a-z0-9._-]*) \| ([0-9][^ |]*) \|", NOTICES, re.MULTILINE)
    return dict(rows)


def test_every_locked_package_is_listed_with_its_locked_version() -> None:
    assert _listed() == _locked()


def test_the_data_sources_are_credited() -> None:
    assert "Dallas Central Appraisal District" in NOTICES
    assert "Only the\nlayout is used" in NOTICES
    assert "never committed" in NOTICES
    assert "RentCast" in NOTICES
    assert "agent-core" in NOTICES


def test_the_readme_links_to_the_notices_from_its_license_section() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    license_section = readme.split("## License", 1)[1]

    assert "(THIRD_PARTY_NOTICES.md)" in license_section

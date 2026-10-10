"""A sample brief for the delivery tests: the three scenarios of the golden fixtures as one brief,
with no database and no model (see scripts/build_brief_fixtures.py)."""

import importlib.util
from pathlib import Path
from typing import Any

from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.proforma.model import ProformaResult

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_brief_fixtures.py"


def sample() -> tuple[Brief, dict[str, tuple[BriefCandidate, ProformaResult]]]:
    spec = importlib.util.spec_from_file_location("build_brief_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    brief, entries = module.sample_brief()
    found: dict[str, Any] = entries
    return brief, found

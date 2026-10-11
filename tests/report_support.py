"""Load scripts/build_eval_report.py as a module (it is a script, not part of the package)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_eval_report.py"


def load_report_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_eval_report", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

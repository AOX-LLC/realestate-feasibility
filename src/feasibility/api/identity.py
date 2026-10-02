"""Which revision of the code this process is running, read once at start-up.

The process imports its code once, so the commit it reports is the one it loaded
(`process_start`), even if the working tree moves on afterwards.
"""

import os
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from feasibility.config import REPO_ROOT

GIT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class ProcessIdentity:
    commit: str | None
    branch: str | None
    version: str


def _git(*args: str) -> str | None:
    if not (REPO_ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["git", "-C", str(REPO_ROOT), *args],  # noqa: S607 - git from PATH
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _package_version() -> str:
    try:
        return version("realestate-feasibility")
    except PackageNotFoundError:
        return "unknown"


def read_identity() -> ProcessIdentity:
    """Image builds pass GIT_COMMIT and GIT_BRANCH; a checkout asks git. Unknown stays None
    rather than being guessed."""
    commit = os.environ.get("GIT_COMMIT") or _git("rev-parse", "--short", "HEAD")
    branch = os.environ.get("GIT_BRANCH") or _git("rev-parse", "--abbrev-ref", "HEAD")
    return ProcessIdentity(commit=commit, branch=branch, version=_package_version())

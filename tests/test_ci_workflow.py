"""The CI workflow parses, and pull requests to any base branch get its checks.

An unquoted `run:` value with a colon and a space in it (`-H "Authorization: Bearer ..."`) is a
YAML error: GitHub then runs nothing at all and reports no checks, which is how stacked pull
requests went unchecked.
"""

from typing import Any

import yaml

from feasibility.config import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers() -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    return _workflow().get("on") or _workflow()[True]


def test_the_workflow_parses_and_every_job_has_steps() -> None:
    jobs = _workflow()["jobs"]

    assert {"lint", "test", "gitleaks", "compose-smoke"} <= set(jobs)
    assert all(job["steps"] for job in jobs.values())


def test_pull_requests_to_any_base_branch_run_the_checks() -> None:
    triggers = _triggers()

    assert "pull_request" in triggers
    # No base-branch filter: a stacked pull request has a feature branch as its base.
    assert not (triggers["pull_request"] or {}).get("branches")
    assert not (triggers["pull_request"] or {}).get("branches-ignore")


def test_pushes_are_checked_on_main_only_so_a_branch_with_a_pull_request_runs_once() -> None:
    assert _triggers()["push"]["branches"] == ["main"]

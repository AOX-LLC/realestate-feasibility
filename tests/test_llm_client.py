"""The client builder, the import boundary, and the per-call cap against a normal call."""

import ast
import subprocess
import sys
from decimal import Decimal

import pytest
from aox_agent_core import AgentClient, Mode, PromptRef, Tier
from aox_agent_core.errors import BudgetExceededError, ReplayMissError
from aox_agent_core.models.pricing import estimate_input_tokens, worst_case_cost
from pydantic import BaseModel, SecretStr

from feasibility.config import REPO_ROOT, DataMode, LlmMode, Settings
from feasibility.llm.client import build_model_client
from feasibility.llm.errors import LlmNotConfiguredError

SENTINEL = "sentinel-llm-key-5b70c2"
SOURCE_ROOT = REPO_ROOT / "src" / "feasibility"
LIBRARY_ROOTS = {"aox_agent_core", "anthropic"}
MAY_IMPORT_THE_LIBRARY = {"llm", "evals"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGENT_CORE_ANTHROPIC_API_KEY",
        "AGENT_CORE_MODE",
        "AGENT_CORE_CONFIG",
        "AGENT_CORE_AUDIT_DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "DATA_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


class Verdict(BaseModel):
    label: str


def test_replay_builds_a_client_with_no_key() -> None:
    client = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]

    assert isinstance(client, AgentClient)
    assert client.config.mode is Mode.REPLAY


def test_the_client_takes_its_mode_from_settings_not_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_CORE_AUDIT_DATABASE_URL", "sqlite:///elsewhere.db")

    client = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]

    assert client.config.audit.database_url is None


def test_record_mode_builds_with_the_settings_key_and_no_network() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        AGENT_CORE_MODE=LlmMode.RECORD,
        AGENT_CORE_ANTHROPIC_API_KEY=SecretStr(SENTINEL),
    )

    client = build_model_client(settings)

    assert client.config.mode is Mode.RECORD
    assert SENTINEL not in repr(client.config)


def test_live_data_with_no_model_configured_builds_no_client() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_mode=DataMode.LIVE, rentcast_api_key=SecretStr("rentcast-sentinel")
    )

    with pytest.raises(LlmNotConfiguredError):
        build_model_client(settings)


def test_only_the_llm_and_evals_packages_import_the_library_or_its_sdk() -> None:
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        package = path.relative_to(SOURCE_ROOT).parts[0]
        if package in MAY_IMPORT_THE_LIBRARY:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name.split(".")[0] in LIBRARY_ROOTS for name in names):
                offenders.append(str(path.relative_to(SOURCE_ROOT)))

    assert offenders == []


def test_nothing_outside_those_packages_imports_by_name_at_run_time() -> None:
    # importlib.import_module("anthropic") would get round the walk above, so dynamic imports are
    # not allowed outside the two packages at all.
    dynamic = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.relative_to(SOURCE_ROOT).parts[0] in MAY_IMPORT_THE_LIBRARY:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                called = ast.unparse(node.func)
                if called in {"__import__", "import_module", "importlib.import_module"}:
                    dynamic.append(str(path.relative_to(SOURCE_ROOT)))

    assert dynamic == []


def test_importing_the_package_in_replay_mode_with_no_key_starts_nothing() -> None:
    program = (
        "import feasibility.llm.client, sys\n"
        "from feasibility.config import Settings\n"
        "from feasibility.llm.client import build_model_client\n"
        "client = build_model_client(Settings(_env_file=None))\n"
        "assert client.config.mode.value == 'replay'\n"
        "sys.exit(0)\n"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "HOME": "/nonexistent"},
        cwd="/",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


# The largest inputs the two tasks will send: remarks are capped at 4,000 characters and the
# narrative facts sheet is a few thousand more, each with a system prompt of a few thousand.
@pytest.mark.parametrize(
    ("task", "tier", "characters"),
    [("signals_extract", Tier.SMALL, 12_000), ("narrative_write", Tier.MID, 14_000)],
)
def test_a_largest_normal_call_passes_the_budget_gate_with_room_for_a_retry(
    task: str, tier: Tier, characters: int
) -> None:
    client = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]
    prompt = PromptRef(
        id="budget.check", version=1, system="s" * (characters // 2), template="${text}"
    )

    # No recording exists, so a call that clears the budget gate is a replay miss: the miss
    # proves the router accepted the call, and nothing was sent anywhere.
    with pytest.raises(ReplayMissError):
        client.call_sync(
            prompt, inputs={"text": "t" * (characters // 2)}, output=Verdict, task=task
        )

    tier_config = client.config.routing.tiers[tier]
    price = client.config.price_for(tier_config.provider, tier_config.model)
    one_attempt = worst_case_cost(
        estimate_input_tokens("x" * characters), tier_config.max_tokens, price
    )
    assert one_attempt * 2 <= Decimal("0.05")


def test_an_oversized_call_is_refused_before_anything_is_sent() -> None:
    client = build_model_client(Settings(_env_file=None))  # type: ignore[call-arg]
    prompt = PromptRef(id="budget.check", version=1, template="${text}")

    with pytest.raises(BudgetExceededError):
        client.call_sync(
            prompt, inputs={"text": "t" * 400_000}, output=Verdict, task="narrative_write"
        )

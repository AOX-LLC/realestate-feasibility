"""Model settings: which modes each data mode allows, where the key may come from, and that the
key is redacted wherever it could be logged."""

import logging
import tomllib
from decimal import Decimal

import pytest
from aox_agent_core import Tier, load_config
from pydantic import SecretStr, ValidationError

from feasibility.config import DEFAULT_LLM_CONFIG_PATH, REPO_ROOT, DataMode, LlmMode, Settings
from feasibility.llm.client import build_model_client
from feasibility.logging import REDACTED, SecretRedactingFilter

SENTINEL = "sentinel-llm-key-8d21e7"
KEY_VARIABLE = "AGENT_CORE_ANTHROPIC_API_KEY"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        KEY_VARIABLE,
        "AGENT_CORE_MODE",
        "AGENT_CORE_CONFIG",
        "ANTHROPIC_API_KEY",
        "DATA_MODE",
        "RENTCAST_API_KEY",
        "LLM_RUN_BUDGET_USD",
        "LLM_MONTHLY_BUDGET_USD",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(data: DataMode, llm: LlmMode, *, key: str | None = None) -> Settings:
    """Settings from arguments only, ignoring any local .env file."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=data,
        llm_mode=llm,
        llm_api_key=SecretStr(key) if key is not None else None,
        rentcast_api_key=SecretStr("rentcast-sentinel") if data is DataMode.LIVE else None,
    )


def test_defaults_replay_recorded_responses_with_no_key() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_mode is LlmMode.REPLAY
    assert settings.llm_api_key is None
    assert settings.llm_configured
    assert settings.llm_run_budget_usd == Decimal("1.00")
    assert settings.llm_monthly_budget_usd == Decimal("10.00")


def test_the_settings_are_read_from_the_documented_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_CORE_MODE", "record")
    monkeypatch.setenv(KEY_VARIABLE, SENTINEL)
    monkeypatch.setenv("AGENT_CORE_CONFIG", "/somewhere/agent-core.toml")
    monkeypatch.setenv("LLM_RUN_BUDGET_USD", "0.25")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "3.00")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_mode is LlmMode.RECORD
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == SENTINEL
    assert str(settings.llm_config_path) == "/somewhere/agent-core.toml"
    assert settings.llm_run_budget_usd == Decimal("0.25")
    assert settings.llm_monthly_budget_usd == Decimal("3.00")


@pytest.mark.parametrize(
    ("data", "llm", "key", "error"),
    [
        (DataMode.MOCK, LlmMode.REPLAY, None, None),
        (DataMode.MOCK, LlmMode.REPLAY, SENTINEL, None),
        (DataMode.MOCK, LlmMode.RECORD, SENTINEL, None),
        (DataMode.MOCK, LlmMode.RECORD, None, f"needs {KEY_VARIABLE}"),
        (DataMode.MOCK, LlmMode.LIVE, SENTINEL, "needs DATA_MODE=live"),
        (DataMode.LIVE, LlmMode.LIVE, SENTINEL, None),
        (DataMode.LIVE, LlmMode.LIVE, None, f"needs {KEY_VARIABLE}"),
        (DataMode.LIVE, LlmMode.RECORD, SENTINEL, "refused with DATA_MODE=live"),
        (DataMode.LIVE, LlmMode.REPLAY, SENTINEL, "needs AGENT_CORE_MODE=live"),
    ],
)
def test_mode_matrix(data: DataMode, llm: LlmMode, key: str | None, error: str | None) -> None:
    if error is None:
        _settings(data, llm, key=key)
        return

    with pytest.raises(ValidationError, match=error):
        _settings(data, llm, key=key)


def test_live_data_with_no_model_configured_is_allowed_and_skips_the_model_stages() -> None:
    settings = _settings(DataMode.LIVE, LlmMode.REPLAY)

    assert not settings.llm_configured


@pytest.mark.parametrize(
    ("data", "llm"),
    [
        (DataMode.MOCK, LlmMode.REPLAY),
        (DataMode.MOCK, LlmMode.RECORD),
        (DataMode.LIVE, LlmMode.LIVE),
    ],
)
def test_every_allowed_combination_is_configured(data: DataMode, llm: LlmMode) -> None:
    assert _settings(data, llm, key=SENTINEL).llm_configured


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_is_no_key(blank: str) -> None:
    with pytest.raises(ValidationError, match=f"needs {KEY_VARIABLE}"):
        _settings(DataMode.MOCK, LlmMode.RECORD, key=blank)

    assert _settings(DataMode.MOCK, LlmMode.REPLAY, key=blank).llm_api_key is None


def test_the_sdk_variable_alone_resolves_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_api_key is None
    assert settings.secret_values() == []


def test_the_model_mode_values_are_the_libraries_modes() -> None:
    from aox_agent_core import Mode

    assert {mode.value for mode in LlmMode} == {mode.value for mode in Mode}


def test_the_key_is_a_redacted_secret() -> None:
    settings = _settings(DataMode.MOCK, LlmMode.RECORD, key=SENTINEL)

    assert settings.secret_values() == [SENTINEL]
    assert SENTINEL not in repr(settings)
    assert SENTINEL not in str(settings.model_dump())


def test_the_key_is_redacted_from_a_logged_message() -> None:
    settings = _settings(DataMode.MOCK, LlmMode.RECORD, key=SENTINEL)
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "request failed with key %s", (SENTINEL,), None
    )

    SecretRedactingFilter(settings.secret_values()).filter(record)
    formatted = logging.Formatter().format(record)

    assert SENTINEL not in formatted
    assert REDACTED in formatted


def test_both_keys_are_redacted_when_both_are_set() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=DataMode.LIVE,
        llm_mode=LlmMode.LIVE,
        llm_api_key=SecretStr(SENTINEL),
        rentcast_api_key=SecretStr("rentcast-sentinel"),
    )

    assert settings.secret_values() == ["rentcast-sentinel", SENTINEL]


def test_a_negative_budget_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_run_budget_usd=Decimal("-1"))  # type: ignore[call-arg]


# --- the TOML file and the routing it produces ---


def test_the_default_config_path_is_the_committed_file() -> None:
    assert DEFAULT_LLM_CONFIG_PATH == REPO_ROOT / "data" / "llm" / "agent-core.toml"
    assert DEFAULT_LLM_CONFIG_PATH.is_file()


def test_tasks_route_extraction_to_small_and_narratives_to_mid() -> None:
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]

    assert config.routing.tier_for_task("signals_extract") is Tier.SMALL
    assert config.routing.tier_for_task("narrative_write") is Tier.MID
    assert config.routing.default_tier is Tier.SMALL


def test_restating_only_max_tokens_keeps_the_packaged_model_of_each_tier() -> None:
    packaged = load_config(environ={})
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]

    for tier in Tier:
        assert config.routing.tiers[tier].model == packaged.routing.tiers[tier].model
        assert config.routing.tiers[tier].provider == packaged.routing.tiers[tier].provider
    assert config.routing.tiers[Tier.SMALL].max_tokens == 1200
    assert config.routing.tiers[Tier.MID].max_tokens == 1500
    assert (
        config.routing.tiers[Tier.LARGE].max_tokens == packaged.routing.tiers[Tier.LARGE].max_tokens
    )


def test_the_per_call_cap_is_five_cents_and_a_normal_call_does_not_raise_it() -> None:
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]

    assert config.routing.budget_usd_per_call == Decimal("0.05")
    assert config.routing.on_budget_exceeded.value == "raise"
    assert not config.routing.escalate_on_structured_failure


def test_the_recordings_folder_is_anchored_to_the_config_file_not_the_working_directory() -> None:
    config = build_model_client(Settings(_env_file=None)).config  # type: ignore[call-arg]

    assert config.replay.cassette_dir == DEFAULT_LLM_CONFIG_PATH.parent / "replays"
    assert config.replay.on_secret.value == "refuse"
    assert not config.tracing.capture_content


def test_prices_and_model_ids_come_from_the_library_not_from_this_project() -> None:
    document = tomllib.loads(DEFAULT_LLM_CONFIG_PATH.read_text())

    assert "pricing" not in document
    assert all("model" not in tier for tier in document["routing"]["tiers"].values())


def test_no_model_id_appears_in_source() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "src").rglob("*.py")
        if "claude-" in path.read_text()
    ]

    assert offenders == []

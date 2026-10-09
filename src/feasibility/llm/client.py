"""Builds the model client from settings. Everything about routing and prices comes from the
TOML file and agent-core's packaged defaults; no model id appears in code."""

from aox_agent_core import AgentClient, AgentCoreConfig, Mode, load_config

from feasibility.config import LlmMode, Settings
from feasibility.llm.errors import LlmNotConfiguredError

# Maps the settings enum onto the library's, by value (a test asserts they cover the same modes).
_LIBRARY_MODES = {mode: Mode(mode.value) for mode in LlmMode}


def load_llm_config(settings: Settings) -> AgentCoreConfig:
    """The routing, prices and mode the settings select: the TOML file merged over the library's
    defaults. The library reads no environment here: the mode is passed in, so a variable such as
    AGENT_CORE_AUDIT_DATABASE_URL in the process cannot change what the client does."""
    if not settings.llm_configured:
        raise LlmNotConfiguredError("live data has no model configured; set AGENT_CORE_MODE=live")
    mode = _LIBRARY_MODES[settings.llm_mode]
    return load_config(settings.llm_config_path, environ={"AGENT_CORE_MODE": mode.value})


def build_model_client(settings: Settings) -> AgentClient:
    """A client in the settings' mode. Replay is given no key at all; record and live get the key
    from settings, which read it from AGENT_CORE_ANTHROPIC_API_KEY and nowhere else."""
    config = load_llm_config(settings)
    key = settings.llm_api_key if settings.llm_mode.is_billable else None
    return AgentClient(config, api_key=key)

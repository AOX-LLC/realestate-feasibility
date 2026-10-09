"""Runtime settings, read from the environment (and an optional .env file)."""

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://feasibility:feasibility-local-dev@127.0.0.1:4502/feasibility"
)
DEFAULT_LLM_CONFIG_PATH = REPO_ROOT / "data" / "llm" / "agent-core.toml"


class DataMode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class LlmMode(StrEnum):
    """How model calls are served. The values are agent-core's own modes; `feasibility.llm`
    maps this onto its enum, so only that package imports the library."""

    REPLAY = "replay"
    RECORD = "record"
    LIVE = "live"

    @property
    def is_billable(self) -> bool:
        return self is not LlmMode.REPLAY


class Settings(BaseSettings):
    # A validation error quotes its input by default, and here the input includes the keys.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: str = DEFAULT_DATABASE_URL
    data_mode: DataMode = DataMode.MOCK
    market: str = "dallas"

    rentcast_api_key: SecretStr | None = None
    rentcast_monthly_budget: int = Field(default=50, ge=0)
    # Capped at 28 so every month has the anchor day.
    rentcast_billing_anchor_day: int = Field(default=1, ge=1, le=28)
    rentcast_ttl_sale_listings: timedelta = timedelta(hours=20)
    rentcast_ttl_property_records: timedelta = timedelta(days=30)
    rentcast_ttl_value_estimates: timedelta = timedelta(days=7)

    # The key is read from the library's own variable and from nowhere else: the SDK's
    # ANTHROPIC_API_KEY is never consulted, so a key exported for another tool cannot be spent.
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias="AGENT_CORE_ANTHROPIC_API_KEY"
    )
    llm_mode: LlmMode = Field(default=LlmMode.REPLAY, validation_alias="AGENT_CORE_MODE")
    llm_config_path: Path = Field(
        default=DEFAULT_LLM_CONFIG_PATH, validation_alias="AGENT_CORE_CONFIG"
    )
    # Per run (every attempt of it) and per UTC calendar month of billable calls.
    llm_run_budget_usd: Decimal = Field(default=Decimal("1.00"), ge=0)
    llm_monthly_budget_usd: Decimal = Field(default=Decimal("10.00"), ge=0)

    snapshot_dir: Path = REPO_ROOT / "data" / "snapshot"
    # Synthetic RESO records (listing remarks) for mock mode, one `<market>.json` per market.
    mls_dir: Path = REPO_ROOT / "data" / "mls"
    # Operator-downloaded files (county zips) and local reports; gitignored.
    local_dir: Path = REPO_ROOT / "local"

    @field_validator("llm_api_key")
    @classmethod
    def _blank_llm_key_is_unset(cls, key: SecretStr | None) -> SecretStr | None:
        # An empty variable (`AGENT_CORE_ANTHROPIC_API_KEY=` in .env, or compose's empty default)
        # means no key, not a key of zero characters.
        if key is not None and not key.get_secret_value().strip():
            return None
        return key

    @model_validator(mode="after")
    def _check_mode_and_key(self) -> "Settings":
        self._check_llm_mode()
        if self.data_mode is DataMode.MOCK:
            # Mock mode never makes a paid call, so a key that happens to be set is dropped.
            self.rentcast_api_key = None
            return self
        if self.rentcast_api_key is None or not self.rentcast_api_key.get_secret_value():
            raise ValueError(
                "DATA_MODE=live needs RENTCAST_API_KEY; use DATA_MODE=mock without one"
            )
        return self

    def _check_llm_mode(self) -> None:
        """Which model modes each data mode may use.

        Mock data allows replay and record. Live data allows only live: replay would miss on
        real listings, and record would write real data into the repository. Live data with no
        model key at all is also allowed; the model stages then skip (see `llm_configured`).
        """
        mode = self.llm_mode
        if mode.is_billable and self.llm_api_key is None:
            raise ValueError(
                f"AGENT_CORE_MODE={mode.value} needs AGENT_CORE_ANTHROPIC_API_KEY; "
                "use replay without one"
            )
        if self.data_mode is DataMode.MOCK:
            if mode is LlmMode.LIVE:
                raise ValueError(
                    "AGENT_CORE_MODE=live needs DATA_MODE=live; use replay or record with mock data"
                )
        elif mode is LlmMode.RECORD:
            raise ValueError(
                "AGENT_CORE_MODE=record is refused with DATA_MODE=live: it would write real "
                "data into the repository"
            )
        elif mode is LlmMode.REPLAY and self.llm_api_key is not None:
            raise ValueError(
                "DATA_MODE=live needs AGENT_CORE_MODE=live when a model key is set: replay would "
                "miss on real listings"
            )

    @property
    def is_live(self) -> bool:
        return self.data_mode is DataMode.LIVE

    @property
    def llm_configured(self) -> bool:
        """False for live data with no model configured: replay cannot serve real listings."""
        return not (self.is_live and self.llm_mode is LlmMode.REPLAY)

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        keys = (self.rentcast_api_key, self.llm_api_key)
        return [key.get_secret_value() for key in keys if key is not None]


@lru_cache
def get_settings() -> Settings:
    return Settings()

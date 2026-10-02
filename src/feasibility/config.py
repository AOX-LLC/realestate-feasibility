"""Runtime settings, read from the environment (and an optional .env file)."""

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://feasibility:feasibility-local-dev@127.0.0.1:4502/feasibility"
)


class DataMode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    snapshot_dir: Path = REPO_ROOT / "data" / "snapshot"
    # Operator-downloaded files (county zips) and local reports; gitignored.
    local_dir: Path = REPO_ROOT / "local"

    @model_validator(mode="after")
    def _check_mode_and_key(self) -> "Settings":
        if self.data_mode is DataMode.MOCK:
            # Mock mode never makes a paid call, so a key that happens to be set is dropped.
            self.rentcast_api_key = None
            return self
        if self.rentcast_api_key is None or not self.rentcast_api_key.get_secret_value():
            raise ValueError(
                "DATA_MODE=live needs RENTCAST_API_KEY; use DATA_MODE=mock without one"
            )
        return self

    @property
    def is_live(self) -> bool:
        return self.data_mode is DataMode.LIVE

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        if self.rentcast_api_key is None:
            return []
        return [self.rentcast_api_key.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

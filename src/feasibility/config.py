"""Runtime settings, read from the environment (and an optional .env file)."""

import ipaddress
import re
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://feasibility:feasibility-local-dev@127.0.0.1:4502/feasibility"
)
DEFAULT_LLM_CONFIG_PATH = REPO_ROOT / "data" / "llm" / "agent-core.toml"
# A bearer token is 32 to 256 of these: what `openssl rand -hex 32` and most generators produce.
API_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,256}$")


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

    # The API's two static bearer tokens. Unset means every gated route answers 401: there is no
    # setting that turns the gate off. The read token opens every GET; the trigger token opens
    # only POST /triggers/*, so the scheduler holds a token that can read nothing.
    api_read_token: SecretStr | None = None
    api_trigger_token: SecretStr | None = None
    api_reads_per_minute: int = Field(default=120, ge=1, le=100_000)
    api_triggers_per_hour: int = Field(default=12, ge=1, le=10_000)
    # Name of a header that carries the client address (for example CF-Connecting-IP behind a
    # tunnel). Left unset, the socket peer is the client: a header anyone can send must not
    # choose who gets rate-limited or banned.
    api_client_ip_header: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")
    # The proxy addresses (comma-separated IPs or networks) that may set that header. A header
    # from any other peer is ignored, because anyone can send one.
    api_trusted_proxies: str | None = None

    # Secrets this mode ignores but the process environment may still hold (compose passes
    # RENTCAST_API_KEY to every service): kept only so that redaction still covers them.
    _ignored_secrets: list[str] = PrivateAttr(default_factory=list)

    # Where generated media (sample PDFs, screenshots, payload dumps) is written. It must be an
    # absolute path outside this repository, so that none of it can be committed by accident.
    media_out: Path | None = None

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

    @field_validator("media_out", mode="before")
    @classmethod
    def _media_out_is_outside_the_repository(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("MEDIA_OUT must be an absolute path")
        if path.resolve().is_relative_to(REPO_ROOT.resolve()):
            raise ValueError("MEDIA_OUT must be outside the repository")
        return path

    @field_validator(
        "api_read_token",
        "api_trigger_token",
        "api_client_ip_header",
        "api_trusted_proxies",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # An empty variable (compose's empty default, `API_READ_TOKEN=` in .env) means unset.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_trusted_proxies(self) -> "Settings":
        if self.api_trusted_proxies is not None:
            for item in self.api_trusted_proxies.split(","):
                try:
                    ipaddress.ip_network(item.strip(), strict=False)
                except ValueError:
                    raise ValueError(
                        "API_TRUSTED_PROXIES must be comma-separated IP addresses or networks"
                    ) from None
        if self.api_client_ip_header is not None and self.api_trusted_proxies is None:
            raise ValueError(
                "API_CLIENT_IP_HEADER needs API_TRUSTED_PROXIES: say which peers may set it"
            )
        return self

    @model_validator(mode="after")
    def _check_api_tokens(self) -> "Settings":
        # The messages name the variable and never the value.
        for variable, token in (
            ("API_READ_TOKEN", self.api_read_token),
            ("API_TRIGGER_TOKEN", self.api_trigger_token),
        ):
            if token is not None and not API_TOKEN_PATTERN.match(token.get_secret_value()):
                raise ValueError(
                    f"{variable} must be 32 to 256 characters from A-Z a-z 0-9 . _ ~ + / = -"
                )
        if (
            self.api_read_token is not None
            and self.api_trigger_token is not None
            and self.api_read_token.get_secret_value() == self.api_trigger_token.get_secret_value()
        ):
            raise ValueError("API_READ_TOKEN and API_TRIGGER_TOKEN must differ")
        return self

    @model_validator(mode="after")
    def _check_mode_and_key(self) -> "Settings":
        self._check_llm_mode()
        if self.data_mode is DataMode.MOCK:
            # Mock mode never makes a paid call, so a key that happens to be set is dropped
            # (and remembered for redaction: it may still be in the environment).
            if self.rentcast_api_key is not None and self.rentcast_api_key.get_secret_value():
                self._ignored_secrets.append(self.rentcast_api_key.get_secret_value())
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
        keys = (
            self.rentcast_api_key,
            self.llm_api_key,
            self.api_read_token,
            self.api_trigger_token,
        )
        configured = [key.get_secret_value() for key in keys if key is not None]
        return [*configured, *self._ignored_secrets]


@lru_cache
def get_settings() -> Settings:
    return Settings()

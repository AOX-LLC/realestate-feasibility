import logging
import sys

import pytest
from pydantic import SecretStr, ValidationError

from feasibility.config import DataMode, Settings
from feasibility.logging import REDACTED, SecretRedactingFilter, configure_logging

SENTINEL = "sentinel-key-3f9a1c"


def _settings(mode: DataMode, key: SecretStr | None) -> Settings:
    """Settings from arguments only, ignoring any local .env file."""
    return Settings(_env_file=None, data_mode=mode, rentcast_api_key=key)  # type: ignore[call-arg]


def test_mock_mode_is_the_default_and_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_MODE", raising=False)
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.data_mode is DataMode.MOCK
    assert settings.rentcast_api_key is None


def test_mock_mode_ignores_a_key_that_is_present() -> None:
    settings = _settings(DataMode.MOCK, SecretStr(SENTINEL))

    assert settings.rentcast_api_key is None
    assert settings.secret_values() == []


@pytest.mark.parametrize("key", [None, ""])
def test_live_mode_without_a_key_refuses_to_start(key: str | None) -> None:
    secret = SecretStr(key) if key is not None else None
    with pytest.raises(ValidationError, match="needs RENTCAST_API_KEY"):
        _settings(DataMode.LIVE, secret)


def test_live_key_never_appears_in_repr() -> None:
    settings = _settings(DataMode.LIVE, SecretStr(SENTINEL))

    assert SENTINEL not in repr(settings)
    assert SENTINEL not in str(settings.model_dump())


def test_filter_redacts_message_args_and_traceback() -> None:
    record_filter = SecretRedactingFilter([SENTINEL])
    try:
        raise RuntimeError(f"boom {SENTINEL}")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "key=%s", (SENTINEL,), exc_info)

    record_filter.filter(record)
    formatted = logging.Formatter().format(record)

    assert SENTINEL not in formatted
    assert REDACTED in formatted


def test_configured_logging_redacts_child_logger_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging([SENTINEL])

    logging.getLogger("feasibility.some.module").warning("header was %s", SENTINEL)

    captured = capsys.readouterr().err
    assert SENTINEL not in captured
    assert REDACTED in captured
    assert logging.getLogger("httpx").level == logging.WARNING

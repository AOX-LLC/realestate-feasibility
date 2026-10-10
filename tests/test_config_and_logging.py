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
    # Ignored, but still redacted: compose puts it in the container's environment.
    assert settings.secret_values() == [SENTINEL]


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


# --- the API's bearer tokens -------------------------------------------------------------------

READ_TOKEN = "r" * 40  # built at runtime: a literal secret-looking string trips the secret scanner
TRIGGER_TOKEN = "t" * 40


def _api_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_no_api_tokens_is_valid_and_means_nothing_is_open() -> None:
    settings = _api_settings()

    assert settings.api_read_token is None
    assert settings.api_trigger_token is None


def test_both_tokens_are_read_from_their_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("API_TRIGGER_TOKEN", TRIGGER_TOKEN)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.api_read_token is not None
    assert settings.api_read_token.get_secret_value() == READ_TOKEN
    assert settings.api_trigger_token is not None
    assert settings.api_trigger_token.get_secret_value() == TRIGGER_TOKEN


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_token_is_unset(blank: str) -> None:
    assert _api_settings(api_read_token=blank).api_read_token is None


def test_a_short_token_is_refused_by_variable_name_and_not_by_value() -> None:
    short = "s" * 31

    with pytest.raises(ValidationError) as raised:
        _api_settings(api_read_token=short)

    assert "API_READ_TOKEN" in str(raised.value)
    assert short not in str(raised.value)


@pytest.mark.parametrize(
    "bad", ["a" * 31, "a" * 257, "a" * 31 + " ", "a" * 31 + "!", "a" * 31 + "é"]
)
def test_a_token_outside_the_allowed_shape_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="API_TRIGGER_TOKEN"):
        _api_settings(api_trigger_token=bad)


def test_the_two_tokens_must_differ() -> None:
    with pytest.raises(ValidationError, match="must differ") as raised:
        _api_settings(api_read_token=READ_TOKEN, api_trigger_token=READ_TOKEN)

    assert READ_TOKEN not in str(raised.value)


def test_both_tokens_are_in_the_secret_values_and_out_of_the_repr() -> None:
    settings = _api_settings(api_read_token=READ_TOKEN, api_trigger_token=TRIGGER_TOKEN)

    assert {READ_TOKEN, TRIGGER_TOKEN} <= set(settings.secret_values())
    assert READ_TOKEN not in repr(settings)
    assert TRIGGER_TOKEN not in repr(settings)


def test_a_client_address_header_must_be_a_header_name() -> None:
    assert _api_settings(api_client_ip_header="CF-Connecting-IP").api_client_ip_header
    assert _api_settings(api_client_ip_header="").api_client_ip_header is None
    with pytest.raises(ValidationError):
        _api_settings(api_client_ip_header="X-Real IP: 1")


def test_the_rate_limits_have_the_planned_defaults_and_bounds() -> None:
    settings = _api_settings()

    assert (settings.api_reads_per_minute, settings.api_triggers_per_hour) == (120, 12)
    with pytest.raises(ValidationError):
        _api_settings(api_reads_per_minute=0)


def test_media_out_is_unset_by_default_and_must_be_absolute_and_outside_the_repository() -> None:
    from feasibility.config import REPO_ROOT

    assert _api_settings().media_out is None
    assert _api_settings(media_out="").media_out is None
    assert str(_api_settings(media_out="/srv/media-out").media_out) == "/srv/media-out"
    for bad in ("media-out", str(REPO_ROOT / "media-out"), str(REPO_ROOT)):
        with pytest.raises(ValidationError, match="MEDIA_OUT"):
            _api_settings(media_out=bad)

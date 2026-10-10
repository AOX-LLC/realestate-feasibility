"""`verify-rentcast` spends from the same monthly budget as the daily run, so it takes the same
spend lock: while a run is pricing candidates, a manual check does not add to the billed count."""

from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from feasibility import cli
from feasibility.config import DataMode, Settings
from feasibility.sources.rentcast import verify
from feasibility.sources.rentcast.budget import SPEND_LOCK, SpendInProgressError, spend_lock


def live_settings(tmp_path: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=DataMode.LIVE,
        rentcast_api_key=SecretStr("k" * 24),
        local_dir=tmp_path,
    )


class NoNetwork:
    """Stands for the real transport: any use of it is a call that would have been billed."""

    made = 0

    def __init__(self, *_: Any, **__: Any) -> None:
        NoNetwork.made += 1

    def get(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("a request was sent while another spender held the lock")

    def close(self) -> None:
        pass


def lock_held(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT count(*) > 0 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND objid = (hashtextextended(:key, 0) & 4294967295)"
                ),
                SPEND_LOCK,
            ).scalar_one()
        )


def test_verify_stops_without_a_call_while_another_spender_holds_the_lock(
    engine: Engine, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "HttpTransport", NoNetwork)
    NoNetwork.made = 0

    with spend_lock(engine), pytest.raises(SpendInProgressError):
        verify.verify(engine, live_settings(tmp_path))

    assert NoNetwork.made == 0  # the transport was never even built
    assert list(tmp_path.iterdir()) == []  # and no report was written


def test_the_command_exits_two_and_says_so_when_a_spend_is_in_progress(
    engine: Engine, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "HttpTransport", NoNetwork)
    monkeypatch.setattr(cli, "get_settings", lambda: live_settings(tmp_path))
    monkeypatch.setattr(cli, "get_engine", lambda: engine)

    with spend_lock(engine):
        result = CliRunner().invoke(cli.app, ["verify-rentcast"])

    assert result.exit_code == 2
    assert "a spend is in progress; try again" in result.output


def test_verify_holds_the_lock_while_it_calls_and_lets_it_go_after(
    engine: Engine, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    class Probe(NoNetwork):
        def get(self, *_: Any, **__: Any) -> Any:
            seen.append(lock_held(engine))
            raise RuntimeError("stop here")

    monkeypatch.setattr(verify, "HttpTransport", Probe)

    with pytest.raises(RuntimeError, match="stop here"):
        verify.verify(engine, live_settings(tmp_path))

    assert seen == [True]
    assert not lock_held(engine)  # released even though the check failed

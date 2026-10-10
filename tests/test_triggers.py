"""POST /triggers/morning: it validates, then queues a job and does nothing else."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_api import READ_HEADERS, TRIGGER_HEADERS, _settings

from feasibility.api.app import create_app
from feasibility.api.routes import triggers as triggers_module
from feasibility.config import DataMode, Settings


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as test_client:
        yield test_client


def post(client: TestClient, body: dict[str, Any] | None, **kwargs: Any) -> Any:
    return client.post("/triggers/morning", headers=TRIGGER_HEADERS, json=body, **kwargs)


def jobs(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(text("SELECT id, kind, payload, dedupe_key, status FROM job"))
        )


def test_a_trigger_queues_one_morning_job_with_the_resolved_date(
    client: TestClient, engine: Engine
) -> None:
    response = post(client, {"market": "dallas", "as_of": "2026-10-01"})

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "job_id": body["job_id"],
        "already_active": False,
        "market": "dallas",
        "as_of": "2026-10-01",
    }
    rows = jobs(engine)
    assert [(r.kind, r.payload, r.dedupe_key, r.status) for r in rows] == [
        (
            "morning.run",
            {"market": "dallas", "as_of": "2026-10-01"},
            "morning.run:dallas:2026-10-01",
            "queued",
        )
    ]
    assert rows[0].id == body["job_id"]


def test_a_second_trigger_for_the_same_day_queues_nothing_and_says_so(
    client: TestClient, engine: Engine
) -> None:
    first = post(client, {"market": "dallas", "as_of": "2026-10-01"}).json()

    second = post(client, {"market": "dallas", "as_of": "2026-10-01"})

    assert second.status_code == 200
    assert second.json()["already_active"] is True
    assert second.json()["job_id"] is None
    assert len(jobs(engine)) == 1
    assert first["job_id"] is not None


def test_a_different_day_queues_its_own_job(client: TestClient, engine: Engine) -> None:
    post(client, {"market": "dallas", "as_of": "2026-10-01"})
    post(client, {"market": "dallas", "as_of": "2026-10-02"})

    assert len(jobs(engine)) == 2


def test_mock_mode_without_a_date_is_a_422_that_lists_the_dates(
    client: TestClient, engine: Engine
) -> None:
    response = post(client, {"market": "dallas"})

    assert response.status_code == 422
    assert "2026-10-01" in response.json()["detail"]
    assert "2026-10-02" in response.json()["detail"]
    assert jobs(engine) == []


def test_a_date_the_snapshot_lacks_is_a_422(client: TestClient, engine: Engine) -> None:
    response = post(client, {"market": "dallas", "as_of": "2026-11-05"})

    assert response.status_code == 422
    assert jobs(engine) == []


def test_an_unknown_market_is_a_422(client: TestClient) -> None:
    response = post(client, {"market": "atlantis", "as_of": "2026-10-01"})

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown market"}


@pytest.mark.parametrize("market", ["", "Dallas", "../etc", "a" * 40, "dallas; drop"])
def test_a_market_that_is_not_an_id_is_refused_before_anything_is_looked_up(
    client: TestClient, market: str
) -> None:
    assert post(client, {"market": market, "as_of": "2026-10-01"}).status_code == 422


def test_an_earlier_date_than_the_latest_run_is_a_409(client: TestClient, engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sourcing_run (market, as_of, status, sync_status) "
                "VALUES ('dallas', :day, 'completed', 'fresh')"
            ),
            {"day": date(2026, 10, 2)},
        )

    response = post(client, {"market": "dallas", "as_of": "2026-10-01"})

    assert response.status_code == 409
    assert jobs(engine) == []
    assert post(client, {"market": "dallas", "as_of": "2026-10-02"}).status_code == 202


def test_an_extra_field_is_a_422(client: TestClient, engine: Engine) -> None:
    response = post(client, {"market": "dallas", "as_of": "2026-10-01", "force": True})

    assert response.status_code == 422
    assert jobs(engine) == []


def test_a_malformed_body_is_a_422(client: TestClient) -> None:
    response = client.post(
        "/triggers/morning",
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
        content=b"{not json",
    )

    assert response.status_code == 422


def test_a_body_over_four_kilobytes_is_a_413(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/triggers/morning",
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
        content=b'{"market": "dallas", "pad": "' + b"x" * 5000 + b'"}',
    )

    assert response.status_code == 413
    assert jobs(engine) == []


def test_a_chunked_body_over_four_kilobytes_is_a_413_too(
    client: TestClient, engine: Engine
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"market": "dallas", "pad": "'
        for _ in range(6):
            yield b"x" * 1000
        yield b'"}'

    response = client.post(
        "/triggers/morning",
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
        content=chunks(),
    )

    assert response.status_code == 413
    assert jobs(engine) == []


def test_a_small_chunked_body_still_reaches_the_route(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"market": "dallas", '
        yield b'"as_of": "2026-10-01"}'

    response = client.post(
        "/triggers/morning",
        headers={**TRIGGER_HEADERS, "content-type": "application/json"},
        content=chunks(),
    )

    assert response.status_code == 202


def test_the_read_token_and_no_token_cannot_trigger(client: TestClient, engine: Engine) -> None:
    body = {"market": "dallas", "as_of": "2026-10-01"}

    assert client.post("/triggers/morning", json=body).status_code == 401
    assert client.post("/triggers/morning", headers=READ_HEADERS, json=body).status_code == 403
    assert client.get("/triggers/morning", headers=READ_HEADERS).status_code == 405
    assert jobs(engine) == []


def test_live_mode_runs_for_today_and_refuses_another_date(engine: Engine) -> None:
    live = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_mode=DataMode.LIVE,
        rentcast_api_key="x" * 20,
    )
    with TestClient(
        create_app(
            live.model_copy(
                update=_settings().model_dump(include={"api_read_token", "api_trigger_token"})
            ),
            engine,
        ),
        raise_server_exceptions=False,
    ) as client:
        other = post(client, {"market": "dallas", "as_of": "2020-01-01"})

    assert other.status_code == 422


def test_the_trigger_module_cannot_sync_spend_or_call_a_model() -> None:
    forbidden = {
        "run_sourcing",
        "RentCastClient",
        "MeteredClient",
        "build_model_client",
        "default_model",
        "run_signals",
        "run_narratives",
        "aox_agent_core",
        "anthropic",
        "sync_listings",
        "build_registry",
    }

    assert forbidden.isdisjoint(vars(triggers_module))


def test_the_trigger_response_keys_pass_the_name_trap(client: TestClient) -> None:
    import re

    body = post(client, {"market": "dallas", "as_of": "2026-10-01"}).json()

    assert not any(
        re.search(r"owner|mail|phone|email|agent|office|taxpayer|legal", key, re.I) for key in body
    )


def test_the_trigger_routes_take_post_and_nothing_else(engine: Engine) -> None:
    paths = create_app(_settings(), engine).openapi()["paths"]
    triggers = {
        path: set(methods) for path, methods in paths.items() if path.startswith("/triggers/")
    }

    assert triggers == {"/triggers/morning": {"post"}, "/triggers/retention": {"post"}}


def test_the_trigger_takes_the_markets_run_lock_before_it_checks_the_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_lock = triggers_module.sourcing_store.lock_market_runs
    real_latest = triggers_module.sourcing_store.latest_run_as_of

    def lock(connection: Any, market: str) -> None:
        order.append("lock")
        real_lock(connection, market)

    def latest(connection: Any, market: str) -> Any:
        order.append("check")
        return real_latest(connection, market)

    monkeypatch.setattr(triggers_module.sourcing_store, "lock_market_runs", lock)
    monkeypatch.setattr(triggers_module.sourcing_store, "latest_run_as_of", latest)

    assert post(client, {"market": "dallas", "as_of": "2026-10-01"}).status_code == 202
    assert order == ["lock", "check"]

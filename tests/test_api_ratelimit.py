"""Rate limits and the failed-authentication ban, on a fake clock."""

import ipaddress

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_api import READ_BEARER, READ_HEADERS, TRIGGER_HEADERS, _settings

from feasibility.api.app import create_app
from feasibility.api.gate import client_address
from feasibility.api.ratelimit import Ban, WindowCounter

WRONG = {"Authorization": "Bearer " + "w" * 40}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- the counters -------------------------------------------------------------------------------


def test_a_counter_refuses_the_hit_past_its_limit_and_resets_with_the_window() -> None:
    clock = Clock()
    counter = WindowCounter(3, 60, clock)

    assert [counter.hit("k").allowed for _ in range(3)] == [True, True, True]
    refused = counter.hit("k")
    assert not refused.allowed
    assert 1 <= refused.retry_after_s <= 61
    clock.now += 60
    assert counter.hit("k").allowed


def test_keys_are_counted_apart() -> None:
    counter = WindowCounter(1, 60, Clock())

    assert counter.hit("a").allowed
    assert counter.hit("b").allowed
    assert not counter.hit("a").allowed


def test_the_number_of_keys_is_bounded_and_the_oldest_is_forgotten() -> None:
    counter = WindowCounter(1, 60, Clock(), max_keys=100)

    for number in range(1000):
        counter.hit(f"address-{number}")

    assert len(counter) == 100
    assert counter.hit("address-0").allowed  # forgotten, so counted afresh
    assert not counter.hit("address-999").allowed  # recent, still counted


def test_a_ban_starts_after_the_failure_limit_and_ends_with_its_time() -> None:
    clock = Clock()
    ban = Ban(failures=3, window_s=600, ban_s=900, clock=clock)

    for _ in range(3):
        ban.record_failure("1.2.3.4")
    assert ban.retry_after_s("1.2.3.4") == 0
    ban.record_failure("1.2.3.4")
    assert 890 <= ban.retry_after_s("1.2.3.4") <= 901
    assert ban.retry_after_s("5.6.7.8") == 0
    clock.now += 901
    assert ban.retry_after_s("1.2.3.4") == 0


# --- through the app -----------------------------------------------------------------------------


def _client(engine: Engine, clock: Clock, **settings: object) -> TestClient:
    configured = _settings().model_copy(update=settings)
    # The peer is a real address, so that a trusted-proxy setting can name it.
    return TestClient(
        create_app(configured, engine, clock=clock),
        raise_server_exceptions=False,
        client=("10.1.2.3", 5000),
    )


def test_the_121st_read_in_a_minute_is_a_429_with_retry_after(engine: Engine) -> None:
    clock = Clock()
    with _client(engine, clock) as client:
        statuses = [client.get("/livez").status_code for _ in range(3)]
        reads = [client.get("/jobs", headers=READ_HEADERS).status_code for _ in range(121)]

        assert statuses == [200, 200, 200]
        assert reads[:120] == [200] * 120
        assert reads[120] == 429
        refused = client.get("/jobs", headers=READ_HEADERS)
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) >= 1
        assert refused.headers["x-content-type-options"] == "nosniff"
        clock.now += 61
        assert client.get("/jobs", headers=READ_HEADERS).status_code == 200


def test_the_read_and_trigger_limits_are_separate(engine: Engine) -> None:
    clock = Clock()
    with _client(engine, clock, api_reads_per_minute=2) as client:
        client.get("/jobs", headers=READ_HEADERS)
        client.get("/jobs", headers=READ_HEADERS)

        assert client.get("/jobs", headers=READ_HEADERS).status_code == 429
        # The trigger scope is another counter; it reaches the route (no such trigger: 404).
        assert client.post("/triggers/none", headers=TRIGGER_HEADERS, json={}).status_code != 429


def test_livez_is_limited_per_address(engine: Engine) -> None:
    clock = Clock()
    with _client(engine, clock) as client:
        codes = [client.get("/livez").status_code for _ in range(61)]

    assert codes[:60] == [200] * 60
    assert codes[60] == 429


def test_twenty_failures_ban_the_address_even_for_a_valid_token(engine: Engine) -> None:
    clock = Clock()
    with _client(engine, clock) as client:
        for _ in range(20):
            assert client.get("/parcels", headers=WRONG).status_code == 401
        assert client.get("/parcels", headers=WRONG).status_code == 401  # the 21st starts the ban
        banned = client.get("/parcels", headers=READ_HEADERS)
        assert banned.status_code == 429
        assert 890 <= int(banned.headers["retry-after"]) <= 901
        assert client.get("/livez").status_code == 200  # the open route is not banned
        clock.now += 901
        assert client.get("/parcels", headers=READ_HEADERS).status_code == 200


def test_good_requests_do_not_count_toward_a_ban(engine: Engine) -> None:
    clock = Clock()
    with _client(engine, clock, api_reads_per_minute=1000) as client:
        for _ in range(100):
            assert client.get("/jobs", headers=READ_HEADERS).status_code == 200
        assert client.get("/jobs", headers=WRONG).status_code == 401


# --- which address is the client -------------------------------------------------------------


TRUSTED = (ipaddress.ip_network("10.0.0.0/8"),)


def test_a_header_from_a_peer_that_is_not_a_trusted_proxy_is_ignored() -> None:
    scope = _scope("192.0.2.7", {"CF-Connecting-IP": "9.9.9.9"})

    assert client_address(scope, "CF-Connecting-IP", TRUSTED) == "192.0.2.7"


def _scope(peer: str | None, headers: dict[str, str]) -> dict[str, object]:
    return {
        "client": (peer, 1234) if peer else None,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


def test_the_peer_is_the_client_when_no_header_is_configured() -> None:
    scope = _scope("10.0.0.1", {"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"})

    assert client_address(scope, None) == "10.0.0.1"
    assert client_address(scope, "CF-Connecting-IP") == "10.0.0.1"  # no trusted proxy named


def test_the_configured_header_is_used_when_it_holds_an_address() -> None:
    scope = _scope("10.0.0.1", {"CF-Connecting-IP": "9.9.9.9"})

    assert client_address(scope, "CF-Connecting-IP", TRUSTED) == "9.9.9.9"
    assert (
        client_address(
            _scope("10.0.0.1", {"CF-Connecting-IP": "2001:db8::1"}), "cf-connecting-ip", TRUSTED
        )
        == "2001:db8::1"
    )


@pytest.mark.parametrize("value", ["not-an-ip", "", "1.2.3.4, 5.6.7.8", "999.1.1.1", "1.2.3.4:80"])
def test_a_configured_header_that_is_not_an_address_falls_back_to_the_peer(value: str) -> None:
    scope = _scope("10.0.0.1", {"CF-Connecting-IP": value})

    assert client_address(scope, "CF-Connecting-IP", TRUSTED) == "10.0.0.1"


def test_no_peer_and_no_header_is_still_a_key() -> None:
    assert client_address(_scope(None, {}), None) == "unknown"


def test_a_spoofed_header_cannot_be_used_to_ban_someone_else_unless_configured(
    engine: Engine,
) -> None:
    clock = Clock()
    with _client(engine, clock) as client:
        for _ in range(25):
            client.get("/parcels", headers={**WRONG, "X-Forwarded-For": "7.7.7.7"})
        # The test client's own address was counted, not 7.7.7.7: it is banned now.
        assert client.get("/parcels", headers=READ_HEADERS).status_code == 429

    with _client(
        engine, clock, api_client_ip_header="X-Forwarded-For", api_trusted_proxies="0.0.0.0/0"
    ) as client:
        for _ in range(25):
            client.get("/parcels", headers={**WRONG, "X-Forwarded-For": "7.7.7.7"})
        ok = {**READ_HEADERS, "X-Forwarded-For": "6.6.6.6"}
        assert client.get("/parcels", headers=ok).status_code == 200
        banned = {**READ_HEADERS, "X-Forwarded-For": "7.7.7.7"}
        assert client.get("/parcels", headers=banned).status_code == 429


def test_the_token_constant_is_not_a_literal_in_this_file() -> None:
    assert READ_BEARER == "r" * 40


def test_requests_with_no_credential_are_limited_and_logged_once_a_minute(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    caplog.set_level("WARNING")
    with _client(engine, clock) as client:
        codes = [client.get("/favicon.ico").status_code for _ in range(70)]
        lines = [r for r in caplog.records if "auth failed" in r.getMessage()]
        clock.now += 61
        later = client.get("/favicon.ico").status_code

    assert codes[:60] == [401] * 60
    assert set(codes[60:]) == {429}
    assert len(lines) == 1
    assert later == 401

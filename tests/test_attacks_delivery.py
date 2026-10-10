"""Attack tests for what delivery exposes: the ledger, the at-most-once post, the errors it keeps,
two deliveries at once, and the deliveries endpoint.

Written before the feature, as strict `xfail`s that failed on a missing module; they pass now.
"""

import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from brief_support import DAY_ONE, DAY_TWO, keys_matching, quoting_model
from conftest import empty_database
from delivery_harness import (
    Faults,
    GateSlack,
    LossyNotion,
    LossySlack,
    clear_ledger,
    deliver,
    ledger,
    paths,
    settings,
    status_of,
    statuses,
)
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from test_api import READ_HEADERS, TRIGGER_HEADERS, _settings

from feasibility.api.app import create_app
from feasibility.delivery.notion import MockNotionTransport
from feasibility.delivery.slack import MockSlackTransport
from feasibility.snapshot.load import seed
from feasibility.sourcing.run import SourcingResult, run_sourcing

TOKEN = "xoxb-not-a-real-credential"
UPLOAD_URL = "https://files.slack.com/upload/v1/" + "u" * 24


@pytest.fixture(scope="module")
def days(migrated_engine: Engine) -> Iterator[tuple[Engine, SourcingResult, SourcingResult]]:
    empty_database(migrated_engine)
    seed(migrated_engine, _settings())
    model = quoting_model()
    one = run_sourcing(migrated_engine, _settings(), "dallas", DAY_ONE, model=model)
    two = run_sourcing(migrated_engine, _settings(), "dallas", DAY_TWO, model=model)
    yield migrated_engine, one, two
    empty_database(migrated_engine)


@pytest.fixture(autouse=True)
def fresh_ledger(request: pytest.FixtureRequest) -> None:
    if "days" in request.fixturenames:
        engine: Engine = request.getfixturevalue("days")[0]
        clear_ledger(engine)


def slack_requests(slack: Any) -> list[str]:
    return paths(slack)


# --- a post whose outcome is unknown is never repeated ------------------------------------------


def test_d1_a_digest_whose_reply_is_lost_is_never_posted_again(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryUnknownOutcomeError

    engine, one, _ = days
    notion, slack = MockNotionTransport(), LossySlack(Faults(lose_reply=["/chat.postMessage"]))

    with pytest.raises(DeliveryUnknownOutcomeError) as first:
        deliver(engine, one.run_id, notion, slack)

    report = first.value.report
    assert status_of(report, "slack", "digest") == "unknown"
    assert slack_requests(slack).count("/chat.postMessage") == 1
    # No file goes anywhere without the digest's thread.
    assert not [p for p in slack_requests(slack) if "Upload" in p or p == "upload"]
    # Notion is a different service: its rows were delivered.
    assert statuses(report, "notion") == {"sent"}
    seen = len(slack.requests)
    again = LossySlack()

    with pytest.raises(DeliveryUnknownOutcomeError):
        deliver(engine, one.run_id, notion, again)

    assert seen == len(slack.requests)
    assert again.requests == []  # not one request, not even a harmless one


def test_d2_a_server_error_on_the_post_is_also_unknown_not_a_retry(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryUnknownOutcomeError

    engine, one, _ = days
    slack = LossySlack(Faults(statuses={"/chat.postMessage": [503]}))

    with pytest.raises(DeliveryUnknownOutcomeError) as caught:
        deliver(engine, one.run_id, MockNotionTransport(), slack)

    assert status_of(caught.value.report, "slack", "digest") == "unknown"
    assert slack_requests(slack).count("/chat.postMessage") == 1


def test_d3_a_rate_limited_post_was_not_processed_so_it_is_waited_out_and_posted_once(
    days: Any,
) -> None:
    engine, one, _ = days
    slack = LossySlack(Faults(statuses={"/chat.postMessage": [429, 429]}))

    report = deliver(engine, one.run_id, MockNotionTransport(), slack)

    assert status_of(report, "slack", "digest") == "sent"
    assert len(slack.posted) == 1
    assert slack_requests(slack).count("/chat.postMessage") == 3


def test_d4_the_only_way_past_an_unknown_post_is_an_explicit_resend(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryUnknownOutcomeError

    engine, one, _ = days
    lost = LossySlack(Faults(lose_reply=["/chat.postMessage"]))
    with pytest.raises(DeliveryUnknownOutcomeError):
        deliver(engine, one.run_id, MockNotionTransport(), lost)
    slack = MockSlackTransport()

    report = deliver(engine, one.run_id, MockNotionTransport(), slack, resend=frozenset({"slack"}))

    assert status_of(report, "slack", "digest") == "sent"
    assert len(slack.posted) == 1
    assert len(slack.uploads) == 5
    digest = [r for r in ledger(engine, one.run_id) if r["item"] == "digest"]
    assert [r["status"] for r in digest] == ["sent"]


# --- two deliveries at once ---------------------------------------------------------------------


def test_d5_two_deliveries_of_one_run_at_once_post_one_digest(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryBusyError

    engine, one, _ = days
    held = GateSlack()
    outcome: list[Any] = []

    def first() -> None:
        try:
            outcome.append(deliver(engine, one.run_id, MockNotionTransport(), held))
        except Exception as error:  # the test reads it below
            outcome.append(error)

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert held.entered.wait(30), "the first delivery never reached the post"
        rival = MockSlackTransport()

        with pytest.raises(DeliveryBusyError):
            deliver(engine, one.run_id, MockNotionTransport(), rival)

        assert rival.requests == []
    finally:
        held.release.set()
        worker.join(60)

    assert len(held.posted) == 1
    assert not isinstance(outcome[0], Exception), outcome[0]
    assert status_of(outcome[0], "slack", "digest") == "sent"


def test_d6_a_left_over_sending_row_is_unknown_once_it_is_ten_minutes_old(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryBusyError, DeliveryUnknownOutcomeError

    engine, one, _ = days
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO delivery (run_id, target, item, mode, status, content_sha256, "
                "attempts) VALUES (:r, 'slack', 'digest', 'mock', 'sending', :h, 1)"
            ),
            {"r": one.run_id, "h": "a" * 64},
        )
    slack = MockSlackTransport()

    # A process that is not running holds no lock, but the row is too new to call.
    with pytest.raises(DeliveryBusyError):
        deliver(engine, one.run_id, MockNotionTransport(), slack)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE delivery SET updated_at = now() - :age WHERE item = 'digest'"),
            {"age": timedelta(minutes=11)},
        )

    with pytest.raises(DeliveryUnknownOutcomeError):
        deliver(engine, one.run_id, MockNotionTransport(), slack)

    assert slack.requests == []


# --- what the ledger and the errors keep --------------------------------------------------------


def test_d7_the_ledger_refuses_values_that_are_not_what_it_is_for(days: Any) -> None:
    engine, one, _ = days
    good = {
        "r": one.run_id,
        "target": "slack",
        "item": "digest",
        "mode": "mock",
        "status": "sent",
        "h": "b" * 64,
        "ref": "1700000000.000100",
        "code": None,
    }
    insert = text(
        "INSERT INTO delivery (run_id, target, item, mode, status, content_sha256, remote_ref, "
        "error_code, attempts) VALUES (:r, :target, :item, :mode, :status, :h, :ref, :code, 1)"
    )
    hostile = [
        {"target": "email"},
        {"item": "row:1; DROP TABLE delivery"},
        {"item": "row:" + "9" * 40},
        {"mode": "staging"},
        {"status": "ok"},
        {"h": "not-a-hash"},
        {"ref": UPLOAD_URL},  # an upload URL is a capability, never kept
        {"ref": "x" * 65},
        {"status": "failed", "ref": None, "code": "the service said: " + TOKEN},
        {"status": "failed", "ref": None, "code": "Has Spaces"},
        {"status": "sent", "ref": None},  # a sent item has somewhere it went
    ]

    for change in hostile:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, {**good, **change})
    with engine.begin() as connection:
        connection.execute(insert, good)  # and the unharmed row is accepted
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert, good)  # once per run, target, item and mode


def test_d8_what_a_service_says_is_not_kept_but_its_code_is(days: Any) -> None:
    from feasibility.delivery.errors import DeliveryIncompleteError
    from feasibility.logging import describe_error

    engine, one, _ = days
    notion = LossyNotion(Faults(statuses={"/v1/pages": [400, 400, 400, 400, 400, 400]}))
    slack = LossySlack(Faults(statuses={"/files.getUploadURLExternal": [500] * 12}))
    mock = MockSlackTransport(error=TOKEN)

    # Notion refuses every create and Slack's upload step fails: one delivery, both services.
    with pytest.raises(DeliveryIncompleteError) as raised:
        deliver(engine, one.run_id, notion, slack)
    printed = describe_error(raised.value)
    assert TOKEN not in printed and "files.slack.com" not in printed
    failed = {i.status for i in raised.value.report.items}
    assert failed == {"failed", "sent"}  # the digest and the last file went; the rest said no
    # And a service that puts the token in its own error text: the code is kept, the text is not.
    clear_ledger(engine)
    with pytest.raises(DeliveryIncompleteError) as raised:
        deliver(engine, one.run_id, MockNotionTransport(), mock)
    assert TOKEN not in describe_error(raised.value)
    assert ("slack", "digest", "failed", "unknown") in {
        (r.target, r.item, r.status, r.error_code) for r in raised.value.report.items
    }

    kept = json.dumps([dict(row, updated_at=None, created_at=None) for row in ledger(engine)])
    assert TOKEN not in kept and "files.slack.com" not in kept and "://" not in kept
    assert {r["error_code"] for r in ledger(engine)} <= {
        None,
        "validation_error",
        "http_500",
        "unknown",
        "network_error",
        "invalid_auth",
    }


def test_d9_a_mock_delivery_never_stands_in_for_a_live_one(days: Any) -> None:
    from pydantic import SecretStr

    from feasibility.config import DeliveryMode

    engine, one, _ = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    live = settings(
        delivery_mode=DeliveryMode.LIVE,
        notion_token=SecretStr("ntn_" + "n" * 40),
        notion_database_id="0" * 32,
        slack_bot_token=SecretStr(TOKEN),
        slack_channel_id="C0MOCKCHAN",
    )
    notion, slack = MockNotionTransport(), MockSlackTransport()

    report = deliver(engine, one.run_id, notion, slack, config=live)

    assert statuses(report, "slack") == {"sent"}
    assert len(slack.posted) == 1 and len(notion.pages) == 5
    assert {r["mode"] for r in ledger(engine, one.run_id)} == {"mock", "live"}


def test_d10_a_dry_run_sends_nothing_and_keeps_nothing(days: Any, tmp_path: Any) -> None:
    engine, one, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport()

    report = deliver(engine, one.run_id, notion, slack, dry_run=True, out=tmp_path)

    assert notion.requests == [] and slack.requests == []
    assert ledger(engine) == []
    assert len(list(tmp_path.glob("*.pdf"))) == 5
    assert report.payloads  # what would have been sent, for the operator to read


def test_d11_a_run_whose_brief_cannot_be_built_delivers_nothing(days: Any) -> None:
    from feasibility.delivery.build import BriefError

    engine, one, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE proforma SET profit = profit + 1 WHERE run_id = :r"), {"r": one.run_id}
        )
    try:
        with pytest.raises(BriefError):
            deliver(engine, one.run_id, notion, slack)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE proforma SET profit = profit - 1 WHERE run_id = :r"),
                {"r": one.run_id},
            )

    assert notion.requests == [] and slack.requests == []
    assert ledger(engine) == []


# --- the deliveries endpoint --------------------------------------------------------------------


def test_d12_the_deliveries_endpoint_needs_the_read_token_and_serves_no_payload(days: Any) -> None:
    engine, one, _ = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    url = f"/sourcing/runs/{one.run_id}/deliveries"
    with TestClient(create_app(_settings(), engine), raise_server_exceptions=False) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers=TRIGGER_HEADERS).status_code == 403
        found = client.get(url, headers=READ_HEADERS)
        unknown = client.get("/sourcing/runs/999999/deliveries", headers=READ_HEADERS)

    assert found.status_code == 200 and unknown.status_code == 404
    items = found.json()["items"]
    assert len(items) == 11  # 5 rows, the digest and 5 files
    assert {key for item in items for key in item} == {
        "target",
        "item",
        "mode",
        "status",
        "attempts",
        "error_code",
        "remote_ref",
        "updated_at",
    }
    assert keys_matching(found.json()) == []
    assert "://" not in found.text and "xoxb" not in found.text

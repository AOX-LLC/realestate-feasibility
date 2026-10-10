"""Delivering the snapshot's two days over mock transports: the exact requests, what a repeat
sends, and what each kind of failure leaves in the ledger."""

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from delivery_harness import (
    Faults,
    LossyNotion,
    LossySlack,
    clear_ledger,
    deliver,
    ledger,
    run_two_days,
    status_of,
    statuses,
)
from sqlalchemy import Engine

from feasibility.delivery.errors import (
    DeliveryConfigError,
    DeliveryIncompleteError,
    DeliveryUnknownOutcomeError,
)
from feasibility.delivery.notion import MockNotionTransport
from feasibility.delivery.slack import MockSlackTransport


@pytest.fixture(scope="module")
def days(migrated_engine: Engine) -> Iterator[tuple[Engine, Any, Any]]:
    one, two = run_two_days(migrated_engine)
    yield migrated_engine, one, two
    from conftest import empty_database

    empty_database(migrated_engine)


@pytest.fixture(autouse=True)
def fresh_ledger(days: Any) -> None:
    clear_ledger(days[0])


def kinds(transport: Any) -> Counter[str]:
    """The requests a transport saw, as `METHOD path` with page ids folded."""
    return Counter(
        f"{r.method} {re.sub(r'mock-page-[0-9a-f]+', 'PAGE', r.path)}" for r in transport.requests
    )


NOTION_DAY_ONE = {
    "GET /v1/databases/" + "0" * 32: 1,
    "POST /v1/databases/" + "0" * 32 + "/query": 5,
    "POST /v1/pages": 5,
}
SLACK_FILE = {
    "POST /files.getUploadURLExternal": 1,
    "POST upload": 1,
    "POST /files.completeUploadExternal": 1,
}


def slack_kinds(files: int) -> Counter[str]:
    found: Counter[str] = Counter({"POST /chat.postMessage": 1})
    for name, count in SLACK_FILE.items():
        found[name] += count * files
    return found


# --- the first delivery, the next day, and a repeat ---------------------------------------------


def test_day_one_sends_five_rows_a_digest_and_five_files(days: Any) -> None:
    engine, one, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport()

    report = deliver(engine, one.run_id, notion, slack)

    assert kinds(notion) == Counter(NOTION_DAY_ONE) and len(notion.requests) == 11
    assert kinds(slack) == slack_kinds(5) and len(slack.requests) == 16
    assert report.counts() == {"sent": 11}
    assert report.dry_run is False and report.mode == "mock"
    rows = ledger(engine, one.run_id)
    assert {r["status"] for r in rows} == {"sent"} and len(rows) == 11
    assert all(r["attempts"] == 1 and r["error_code"] is None for r in rows)


def test_a_second_delivery_of_the_same_day_sends_nothing_at_all(days: Any) -> None:
    engine, one, _ = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    notion, slack = MockNotionTransport(), MockSlackTransport()

    report = deliver(engine, one.run_id, notion, slack)

    assert notion.requests == [] and slack.requests == []
    assert report.counts() == {"skipped": 11}
    assert {r["status"] for r in ledger(engine, one.run_id)} == {"sent"}  # the first send stays


def test_day_two_updates_the_pages_it_remembers_and_creates_the_new_row(days: Any) -> None:
    engine, one, two = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    notion, slack = MockNotionTransport(), MockSlackTransport()

    deliver(engine, two.run_id, notion, slack)

    kind = kinds(notion)
    assert kind["GET /v1/databases/" + "0" * 32] == 1
    assert kind["PATCH /v1/pages/PAGE"] == 5  # five candidates are on both days
    assert kind["POST /v1/pages"] == 1  # and one is new
    assert kind["POST /v1/databases/" + "0" * 32 + "/query"] == 1  # only the new one is looked up
    assert len(notion.requests) == 8
    assert kinds(slack) == slack_kinds(6) and len(slack.requests) == 19
    pages = {r["item"]: r["remote_ref"] for r in ledger(engine) if r["target"] == "notion"}
    assert len(pages) == 6 and len(set(pages.values())) == 6  # six rows, six distinct pages


def test_rows_updated_on_day_two_are_the_pages_created_on_day_one(days: Any) -> None:
    engine, one, two = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    first = {
        r["item"]: r["remote_ref"] for r in ledger(engine, one.run_id) if r["target"] == "notion"
    }

    deliver(engine, two.run_id, MockNotionTransport(), MockSlackTransport())

    second = {
        r["item"]: r["remote_ref"] for r in ledger(engine, two.run_id) if r["target"] == "notion"
    }
    assert {item: ref for item, ref in second.items() if item in first} == first


def test_a_page_that_is_gone_is_looked_up_by_its_key_and_the_row_is_written_again(
    days: Any,
) -> None:
    engine, one, two = days
    deliver(engine, one.run_id, MockNotionTransport(), MockSlackTransport())
    gone = LossyNotion(Faults(statuses={"/v1/pages/" + _first_page(engine, one.run_id): [404]}))

    deliver(engine, two.run_id, gone, MockSlackTransport())

    assert len({r["item"] for r in ledger(engine, two.run_id) if r["target"] == "notion"}) == 6
    assert statuses(_report_of(engine, two.run_id), "notion") <= {
        "sent"
    }  # nothing was left half-done


def _first_page(engine: Engine, run_id: int) -> str:
    return next(r["remote_ref"] for r in ledger(engine, run_id) if r["target"] == "notion")


def _report_of(engine: Engine, run_id: int) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        items=[
            SimpleNamespace(target=r["target"], item=r["item"], status=r["status"])
            for r in ledger(engine, run_id)
        ]
    )


# --- failures -----------------------------------------------------------------------------------


def test_a_row_the_service_keeps_refusing_fails_alone_and_the_rest_is_delivered(days: Any) -> None:
    engine, one, _ = days
    notion = LossyNotion(Faults(statuses={"/v1/pages": [None, None, 429, 429, 429, 429]}))
    slack = MockSlackTransport()

    with pytest.raises(DeliveryIncompleteError) as raised:
        deliver(engine, one.run_id, notion, slack)

    report = raised.value.report
    assert report.counts() == {"sent": 10, "failed": 1}
    failed = [r for r in ledger(engine, one.run_id) if r["status"] == "failed"]
    assert [(r["target"], r["error_code"]) for r in failed] == [("notion", "http_429")]
    assert len(slack.posted) == 1 and len(slack.uploads) == 5  # Slack was not held up by Notion


def test_the_next_try_sends_only_the_row_that_failed(days: Any) -> None:
    engine, one, _ = days
    broken = LossyNotion(Faults(statuses={"/v1/pages": [None, None, 429, 429, 429, 429]}))
    with pytest.raises(DeliveryIncompleteError):
        deliver(engine, one.run_id, broken, MockSlackTransport())
    notion, slack = MockNotionTransport(), MockSlackTransport()

    report = deliver(engine, one.run_id, notion, slack)

    assert slack.requests == []  # the digest and the files are not sent again
    assert kinds(notion) == Counter(
        {
            "GET /v1/databases/" + "0" * 32: 1,
            "POST /v1/databases/" + "0" * 32 + "/query": 1,
            "POST /v1/pages": 1,
        }
    )
    assert report.counts() == {"sent": 1, "skipped": 10}
    failed_row = next(r for r in ledger(engine, one.run_id) if r["attempts"] == 2)
    assert failed_row["status"] == "sent" and failed_row["error_code"] is None


def test_a_row_whose_create_may_have_happened_is_found_not_created_twice(days: Any) -> None:
    engine, one, _ = days
    notion = LossyNotion(Faults(lose_reply=["/v1/pages"]))

    with pytest.raises(DeliveryIncompleteError) as raised:
        deliver(engine, one.run_id, notion, MockSlackTransport())

    assert statuses(raised.value.report, "notion") == {"unknown"}
    assert len(notion.pages) == 5  # the service did create them all

    second = deliver(engine, one.run_id, notion, MockSlackTransport())

    assert statuses(second, "notion") == {"sent"}
    assert len(notion.pages) == 5  # found by their keys, updated, not created again


def test_bad_credentials_stop_notion_but_not_slack_and_end_permanent(days: Any) -> None:
    engine, one, _ = days
    notion = LossyNotion(Faults(statuses={"/v1/databases/" + "0" * 32: [401]}))
    slack = MockSlackTransport()

    with pytest.raises(DeliveryConfigError, match="http_401") as raised:
        deliver(engine, one.run_id, notion, slack)

    report = raised.value.report
    assert len(slack.posted) == 1 and len(slack.uploads) == 5
    assert [i.status for i in report.items if i.target == "notion"] == ["failed"] + [
        "not_attempted"
    ] * 4
    assert len(notion.requests) == 1  # one request, then it stopped
    notion_rows = [r for r in ledger(engine, one.run_id) if r["target"] == "notion"]
    assert [(r["status"], r["error_code"]) for r in notion_rows] == [("failed", "http_401")]


def test_a_database_missing_a_property_is_refused_before_any_row_is_written(days: Any) -> None:
    engine, one, _ = days
    notion = MockNotionTransport(missing=("Max offer",))

    with pytest.raises(DeliveryConfigError, match="notion_schema"):
        deliver(engine, one.run_id, notion, MockSlackTransport())

    assert [r.path for r in notion.requests] == ["/v1/databases/" + "0" * 32]


def test_a_channel_slack_does_not_know_stops_slack_and_posts_nothing_else(days: Any) -> None:
    engine, one, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport(error="channel_not_found")

    with pytest.raises(DeliveryConfigError, match="channel_not_found"):
        deliver(engine, one.run_id, notion, slack)

    assert len(notion.pages) == 5
    assert [r.path for r in slack.requests] == ["/chat.postMessage"]
    assert [
        (r["status"], r["error_code"]) for r in ledger(engine, one.run_id) if r["target"] == "slack"
    ] == [("failed", "channel_not_found")]


def test_one_file_that_fails_leaves_the_others_sent_and_is_the_only_one_tried_next(
    days: Any,
) -> None:
    engine, one, _ = days
    slack = LossySlack(Faults(statuses={"/files.getUploadURLExternal": [None, 500, 500, 500]}))

    with pytest.raises(DeliveryIncompleteError) as raised:
        deliver(engine, one.run_id, MockNotionTransport(), slack)

    assert raised.value.report.counts() == {"sent": 10, "failed": 1}
    assert len(slack.uploads) == 4
    again = MockSlackTransport()

    deliver(engine, one.run_id, MockNotionTransport(), again)

    assert kinds(again) == slack_kinds(1) - Counter({"POST /chat.postMessage": 1})
    assert len(again.uploads) == 1


def test_an_upload_whose_completion_is_lost_is_not_retried_and_stops_the_run(days: Any) -> None:
    engine, one, _ = days
    slack = LossySlack(Faults(lose_reply=["/files.completeUploadExternal"]))

    with pytest.raises(DeliveryUnknownOutcomeError) as raised:
        deliver(engine, one.run_id, MockNotionTransport(), slack)

    assert status_of(raised.value.report, "slack", "digest") == "sent"
    unknown = [i for i in raised.value.report.items if i.status == "unknown"]
    assert len(unknown) == 5  # every file's completion was lost; each is left for a person
    completions = [r for r in slack.requests if r.path == "/files.completeUploadExternal"]
    assert len(completions) == 5


def test_only_one_target_can_be_chosen_and_an_unknown_name_is_refused(days: Any) -> None:
    engine, one, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport()

    deliver(engine, one.run_id, notion, slack, only="slack")

    assert notion.requests == [] and len(slack.posted) == 1
    with pytest.raises(ValueError, match="only must be one of"):
        deliver(engine, one.run_id, notion, slack, only="email")
    with pytest.raises(ValueError, match="can be sent again"):
        deliver(engine, one.run_id, notion, slack, resend=frozenset({"notion"}))


def test_a_run_that_does_not_exist_delivers_nothing(days: Any) -> None:
    from feasibility.delivery.build import RunNotFoundError

    engine, _, _ = days
    notion, slack = MockNotionTransport(), MockSlackTransport()

    with pytest.raises(RunNotFoundError):
        deliver(engine, 999999, notion, slack)

    assert notion.requests == [] and slack.requests == [] and ledger(engine) == []


def test_a_configuration_error_and_an_unknown_outcome_are_permanent_and_the_rest_are_retried() -> (
    None
):
    from feasibility.delivery.errors import DeliveryBusyError, NotionError, SlackError
    from feasibility.jobs.handlers import PERMANENT_ERRORS

    for permanent in (DeliveryConfigError, DeliveryUnknownOutcomeError):
        assert issubclass(permanent, PERMANENT_ERRORS)
    # An incomplete delivery retries (the ledger sends only what is left), and so does a busy run
    # and a refusal that another try may fix.
    for retried in (DeliveryIncompleteError, DeliveryBusyError, NotionError, SlackError):
        assert not issubclass(retried, PERMANENT_ERRORS)


def test_a_resend_after_one_lost_file_posts_that_file_again_and_not_the_digest(days: Any) -> None:
    engine, one, _ = days
    slack = LossySlack(Faults(lose_reply=["/files.completeUploadExternal"]))
    with pytest.raises(DeliveryUnknownOutcomeError):
        deliver(engine, one.run_id, MockNotionTransport(), slack)
    again = MockSlackTransport()

    report = deliver(engine, one.run_id, MockNotionTransport(), again, resend=frozenset({"slack"}))

    assert again.posted == []  # the digest that is in the channel is not posted again
    assert len(again.uploads) == 5  # every file's completion had been lost
    assert status_of(report, "slack", "digest") == "skipped"

"""The Notion client: the row it builds, the requests it makes, and what it will not say."""

import json
import logging
import re
from decimal import Decimal
from typing import Any

import httpx
import pytest
from delivery_support import sample

from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.delivery.errors import (
    DeliveryConfigError,
    NotionError,
    NotionOutcomeUnknownError,
    TransportError,
)
from feasibility.delivery.notion import (
    DECISION_PROPERTY,
    NOTION_VERSION,
    OWNED_PROPERTIES,
    HttpNotionTransport,
    MockNotionTransport,
    NotionClient,
    candidate_key,
    row_properties,
)
from feasibility.delivery.transport import Pacer, TransportResponse

DATABASE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TOKEN = "ntn_" + "n" * 40
NAME_TRAP = re.compile(r"owner|mail|phone|email|agent|office|taxpayer|legal", re.IGNORECASE)


@pytest.fixture(scope="module")
def brief_and_entries() -> tuple[Brief, list[BriefCandidate]]:
    brief, entries = sample()
    return brief, [entry for entry, _ in entries.values()]


def client(transport: Any, sleeps: list[float] | None = None) -> NotionClient:
    record = sleeps if sleeps is not None else []
    return NotionClient(
        transport,
        DATABASE,
        pacer=Pacer(0.0, clock=lambda: 0.0, sleep=record.append),
        sleep=record.append,
    )


# --- the row ---------------------------------------------------------------------------------


def test_a_row_has_exactly_the_owned_properties_and_never_the_decision(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    for entry in entries:
        properties = row_properties(brief, entry)

        assert set(properties) == set(OWNED_PROPERTIES)
        assert DECISION_PROPERTY not in properties


def test_every_property_name_and_every_key_passes_the_name_trap(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    def keys(node: Any) -> list[str]:
        if isinstance(node, dict):
            return [*node, *[k for v in node.values() for k in keys(v)]]
        if isinstance(node, list):
            return [k for v in node for k in keys(v)]
        return []

    for name in (*OWNED_PROPERTIES, DECISION_PROPERTY):
        assert not NAME_TRAP.search(name), name
    for entry in entries:
        assert not [k for k in keys(row_properties(brief, entry)) if NAME_TRAP.search(k)]


def test_numbers_come_from_the_decimal_strings_and_the_margin_is_the_fraction(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries
    entry = entries[0]

    properties = row_properties(brief, entry)

    assert Decimal(str(properties["ARV"]["number"])) == Decimal(entry.figures.arv)
    assert Decimal(str(properties["Profit"]["number"])) == Decimal(entry.figures.profit)
    assert Decimal(str(properties["Margin"]["number"])) == Decimal(entry.figures.margin)
    assert properties["Margin"]["number"] < 1
    assert properties["Rank"]["number"] == entry.rank
    assert properties["Max offer"] == {"number": None} or properties["Max offer"]["number"] > 0


def test_the_summary_is_only_an_accepted_one_and_the_text_is_plain(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    accepted = row_properties(brief, entries[0])
    withheld = row_properties(brief, entries[1])
    deferred = row_properties(brief, entries[2])

    assert accepted["Summary"]["rich_text"][0]["text"]["content"] == entries[0].narrative.summary
    assert withheld["Summary"]["rich_text"] == [] and deferred["Summary"]["rich_text"] == []
    assert accepted["Narrative"] == {"select": {"name": "accepted"}}
    assert withheld["Narrative"] == {"select": {"name": "withheld"}}
    assert deferred["Narrative"] == {"select": {"name": "not available"}}
    assert accepted["Data"] == {"select": {"name": "synthetic"}}
    for properties in (accepted, withheld, deferred):
        for value in properties.values():
            for item in value.get("rich_text", []) + value.get("title", []):
                assert set(item) == {"type", "text"} and set(item["text"]) == {"content"}


def test_the_key_is_the_market_and_the_candidate_id(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    assert candidate_key(brief, entries[0]) == f"dallas:{entries[0].candidate_id}"
    key = row_properties(brief, entries[0])["Candidate key"]["rich_text"][0]["text"]["content"]
    assert key == candidate_key(brief, entries[0])


def test_flags_and_signals_are_codes_a_multi_select_can_hold(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    for entry in entries:
        for column in ("Flags", "Signals"):
            for option in row_properties(brief, entry)[column]["multi_select"]:
                assert re.fullmatch(r"[a-z_]+", option["name"])


# --- the client against the mock transport ---------------------------------------------------


def test_a_first_brief_creates_a_row_and_a_second_updates_it(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries
    transport = MockNotionTransport()
    notion = client(transport)

    first = notion.upsert_row(brief, entries[0])
    second = notion.upsert_row(brief, entries[0])

    assert first[1] == "created" and second == (first[0], "updated")
    assert re.fullmatch(r"mock-page-[0-9a-f]{12}", first[0])
    methods = [(r.method, r.path.split("/")[-1]) for r in transport.requests]
    assert methods == [
        ("POST", "query"),
        ("POST", "pages"),
        ("POST", "query"),
        ("PATCH", first[0]),
    ]


def test_a_mock_page_id_comes_from_the_rows_key_so_no_two_rows_or_processes_share_one(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    one = client(MockNotionTransport()).upsert_row(brief, entries[0])[0]
    again = client(MockNotionTransport()).upsert_row(brief, entries[0])[0]
    other = client(MockNotionTransport()).upsert_row(brief, entries[1])[0]

    assert one == again and one != other


def test_the_query_filters_on_the_candidate_key_property(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries
    transport = MockNotionTransport()

    client(transport).find_row(candidate_key(brief, entries[0]))

    sent = transport.requests[0].json
    assert sent == {
        "filter": {
            "property": "Candidate key",
            "rich_text": {"equals": candidate_key(brief, entries[0])},
        },
        "page_size": 1,
    }


def test_check_schema_reports_a_missing_and_a_wrongly_typed_property() -> None:
    transport = MockNotionTransport(missing=("Profit",), wrong_type=("Rank",))

    report = client(transport).check_schema()

    assert report == {"missing": ["Profit"], "wrong_type": ["Rank"]}


def test_setup_adds_only_what_is_missing_and_never_removes_or_renames() -> None:
    transport = MockNotionTransport(missing=("Profit", DECISION_PROPERTY))

    added = client(transport).setup_schema()

    assert added == sorted(["Profit", DECISION_PROPERTY])
    patch = next(r for r in transport.requests if r.method == "PATCH")
    assert set(patch.json["properties"]) == {"Profit", DECISION_PROPERTY}  # type: ignore[index]
    options = patch.json["properties"][DECISION_PROPERTY]["select"]["options"]  # type: ignore[index]
    assert [o["name"] for o in options] == ["Reviewing", "Pass", "Offer"]
    assert client(transport).setup_schema() == []


# --- failures --------------------------------------------------------------------------------


class Scripted:
    """A transport that answers from a list, then repeats its last answer."""

    def __init__(self, *answers: TransportResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    def request(self, method: str, path: str, **kwargs: Any) -> TransportResponse:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_429_is_waited_out_with_the_services_delay_capped_and_tried_three_times() -> None:
    sleeps: list[float] = []
    limited = TransportResponse(429, {"code": "rate_limited"}, {"Retry-After": "9999"})
    transport = Scripted(limited, limited, limited, TransportResponse(200, {"results": []}))

    assert client(transport, sleeps).find_row("k") is None

    assert transport.calls == 4
    assert [s for s in sleeps if s > 0] == [30.0, 30.0, 30.0]


def test_a_fourth_429_is_an_error_carrying_only_its_status() -> None:
    limited = TransportResponse(429, {"code": "rate_limited", "message": "SECRET-TEXT"}, {})
    transport = Scripted(limited)

    with pytest.raises(NotionError) as raised:
        client(transport).find_row("k")

    assert raised.value.code == "http_429"
    assert "SECRET-TEXT" not in str(raised.value)


def test_a_server_error_is_retried_twice_after_one_and_four_seconds() -> None:
    sleeps: list[float] = []
    transport = Scripted(
        TransportResponse(503), TransportResponse(503), TransportResponse(200, {"results": []})
    )

    client(transport, sleeps).find_row("k")

    assert [s for s in sleeps if s > 0] == [1.0, 4.0]
    with pytest.raises(NotionError, match="http_500"):
        client(Scripted(TransportResponse(500))).find_row("k")


def test_a_network_error_is_retried_then_reported_without_its_text() -> None:
    transport = Scripted(TransportError("network_error"))

    with pytest.raises(NotionError, match="network_error"):
        client(transport).find_row("k")

    assert transport.calls == 3


def test_a_400_carries_no_text_from_the_service() -> None:
    body = {"code": "validation_error", "message": "Profit is not a property that exists SECRET"}
    with pytest.raises(NotionError) as raised:
        client(Scripted(TransportResponse(400, body))).create_row({})

    assert str(raised.value) == "validation_error" and "SECRET" not in repr(raised.value)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_a_config_error(status: int) -> None:
    with pytest.raises(DeliveryConfigError):
        client(Scripted(TransportResponse(status, {"code": "unauthorized"}))).find_row("k")


def test_a_missing_database_is_a_config_error_and_a_missing_page_is_not() -> None:
    with pytest.raises(DeliveryConfigError):
        client(Scripted(TransportResponse(404))).check_schema()
    with pytest.raises(NotionError):
        client(Scripted(TransportResponse(404))).update_row("page", {})


# --- the real transport, over httpx's own mock -----------------------------------------------


def http_transport(handler: Any) -> HttpNotionTransport:
    return HttpNotionTransport(
        TOKEN,
        client=httpx.Client(
            base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
        ),
    )


def test_the_token_is_in_the_authorization_header_and_nowhere_else() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    NotionClient(http_transport(handler), DATABASE, pacer=Pacer(0.0)).find_row("k")

    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["notion-version"] == NOTION_VERSION
    assert TOKEN not in str(request.url) and TOKEN not in request.content.decode()
    assert request.url.query == b""


def test_a_network_failure_is_a_transport_error_without_the_urls_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} with {TOKEN}")

    with pytest.raises(TransportError) as raised:
        http_transport(handler).request("GET", "/v1/databases/x")

    assert TOKEN not in str(raised.value) and "api.notion.com" not in str(raised.value)
    assert TOKEN not in json.dumps([r.getMessage() for r in caplog.records])


def test_a_row_is_never_created_twice_after_a_server_error_or_a_lost_connection() -> None:
    for failure in (TransportResponse(503), TransportError("network_error")):
        transport = Scripted(failure)

        with pytest.raises(NotionOutcomeUnknownError) as raised:
            client(transport).create_row({})

        assert transport.calls == 1
        assert isinstance(raised.value, NotionError)


def test_a_created_page_without_an_id_is_an_unknown_outcome() -> None:
    with pytest.raises(NotionOutcomeUnknownError, match="no_page_id"):
        client(Scripted(TransportResponse(200, {}))).create_row({})


def test_a_definite_refusal_of_a_create_is_not_an_unknown_outcome() -> None:
    with pytest.raises(NotionError) as raised:
        client(Scripted(TransportResponse(400, {"code": "validation_error"}))).create_row({})

    assert not isinstance(raised.value, NotionOutcomeUnknownError)


def test_an_update_is_retried_after_a_server_error() -> None:
    transport = Scripted(TransportResponse(503), TransportResponse(200, {}))

    client(transport).update_row("page", {})

    assert transport.calls == 2

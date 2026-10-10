"""The Slack client: the digest it builds, the calls it makes, and what it will not say."""

import json
import logging
from typing import Any

import httpx
import pytest
from delivery_support import sample

from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.delivery.errors import DeliveryConfigError, SlackError, TransportError
from feasibility.delivery.slack import (
    MAX_BLOCKS,
    MAX_TEXT,
    HttpSlackTransport,
    MockSlackTransport,
    SlackClient,
    digest_blocks,
    escape,
    file_name,
    file_title,
)
from feasibility.delivery.transport import Pacer, TransportResponse

CHANNEL = "C0123ABCD45"
TOKEN = "xoxb-not-a-real-credential"


@pytest.fixture(scope="module")
def brief_and_entries() -> tuple[Brief, list[BriefCandidate]]:
    brief, entries = sample()
    return brief, [entry for entry, _ in entries.values()]


def client(transport: Any, sleeps: list[float] | None = None) -> SlackClient:
    record = sleeps if sleeps is not None else []
    return SlackClient(
        transport,
        CHANNEL,
        pacer=Pacer(0.0, clock=lambda: 0.0, sleep=record.append),
        sleep=record.append,
    )


def strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings(v)]
    return [node] if isinstance(node, str) else []


# --- the digest ------------------------------------------------------------------------------


def test_the_digest_has_a_header_a_synthetic_note_a_section_per_candidate_and_a_footer(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    blocks, text = digest_blocks(brief)

    assert blocks[0]["type"] == "header" and "Dallas" in blocks[0]["text"]["text"]
    assert text == blocks[0]["text"]["text"]
    assert "Synthetic demo data" in json.dumps(blocks[1])
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == len(entries) + 1  # one each, and the counted line
    assert "7 more ranked candidates" in sections[-1]["text"]["text"]
    assert blocks[-1]["type"] == "context" and brief.footer in blocks[-1]["elements"][0]["text"]
    assert len(blocks) <= MAX_BLOCKS
    assert all(len(s["text"]["text"]) <= MAX_TEXT for s in sections)


def test_the_accepted_summary_is_in_its_section_and_a_withheld_one_has_only_the_note(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, entries = brief_and_entries

    blocks, _ = digest_blocks(brief)

    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert entries[0].narrative.summary in texts[0]
    assert entries[1].narrative.note in texts[1] and "108k" not in texts[1]
    assert entries[2].narrative.note in texts[2]


def test_every_markup_text_object_says_verbatim(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, _ = brief_and_entries
    blocks, _ = digest_blocks(brief)

    def walk(node: Any) -> list[dict[str, Any]]:
        found = []
        if isinstance(node, dict):
            if node.get("type") == "mrkdwn":
                found.append(node)
            for value in node.values():
                found += walk(value)
        elif isinstance(node, list):
            for value in node:
                found += walk(value)
        return found

    objects = walk(blocks)
    assert objects and all(o["verbatim"] is True for o in objects)


@pytest.mark.parametrize(
    "hostile",
    [
        "<!channel>",
        "<http://x|y>",
        "<@U123ABC>",
        "a & b",
        "100 ELM ST #ANNOUNCEMENTS",
        "*bold* `c`",
    ],
)
def test_data_is_escaped_so_nothing_in_it_is_markup(
    brief_and_entries: tuple[Brief, list[BriefCandidate]], hostile: str
) -> None:
    brief, entries = brief_and_entries
    narrative = entries[0].narrative.model_copy(update={"summary": hostile})
    entry = entries[0].model_copy(update={"narrative": narrative})
    changed = brief.model_copy(update={"candidates": [entry]})

    blocks, text = digest_blocks(changed)

    rendered = " ".join(strings(blocks)) + text
    assert escape(hostile) in rendered
    assert "<!" not in rendered and "<@" not in rendered and "<http" not in rendered
    assert "&" not in rendered.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")


def test_the_escape_is_exactly_slacks() -> None:
    assert escape("a&b<c>d") == "a&amp;b&lt;c&gt;d"


def test_file_names_and_titles_come_from_the_rank_and_id_never_the_address(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    _, entries = brief_and_entries
    entry = entries[0].model_copy(update={"street": "<!channel> ELM ST"})

    assert file_name(entry) == f"pro-forma-rank-{entry.rank}-candidate-{entry.candidate_id}.pdf"
    assert "ELM" not in file_name(entry) + file_title(entry)


# --- the calls -------------------------------------------------------------------------------


def test_a_digest_is_posted_to_the_channel_without_unfurling_or_parsing(
    brief_and_entries: tuple[Brief, list[BriefCandidate]],
) -> None:
    brief, _ = brief_and_entries
    transport = MockSlackTransport()
    blocks, text = digest_blocks(brief)

    ts = client(transport).post_digest(blocks, text)

    assert ts == "mock-ts-1"
    body = transport.requests[0].json
    assert body is not None
    assert body["channel"] == CHANNEL and body["text"] == text and body["blocks"] == blocks
    assert body["unfurl_links"] is False and body["unfurl_media"] is False
    assert body["link_names"] is False and body["parse"] == "none"


def test_a_file_upload_makes_the_three_calls_in_order_with_the_channel_and_thread() -> None:
    transport = MockSlackTransport()

    file_id = client(transport).upload_file("a.pdf", b"%PDF-bytes", "Pro-forma rank 1", "mock-ts-1")

    assert file_id == "mock-file-1"
    assert [r.path for r in transport.requests] == [
        "/files.getUploadURLExternal",
        "upload",
        "/files.completeUploadExternal",
    ]
    assert transport.requests[0].json == {"filename": "a.pdf", "length": 10}
    assert transport.uploads == [b"%PDF-bytes"]
    complete = transport.requests[2].json
    assert complete == {
        "files": [{"id": "mock-file-1", "title": "Pro-forma rank 1"}],
        "channel_id": CHANNEL,
        "thread_ts": "mock-ts-1",
    }


def test_the_upload_url_is_in_no_request_record_and_no_returned_value() -> None:
    transport = MockSlackTransport()

    result = client(transport).upload_file("a.pdf", b"x", "t", "mock-ts-1")

    everything = json.dumps([r.__dict__ for r in transport.requests]) + result
    assert "upload.invalid" not in everything and "http" not in everything


@pytest.mark.parametrize(
    "code", ["invalid_auth", "not_in_channel", "channel_not_found", "missing_scope"]
)
def test_a_configuration_error_is_permanent(code: str) -> None:
    with pytest.raises(DeliveryConfigError):
        client(MockSlackTransport(error=code)).post_digest([], "t")


def test_another_error_is_a_slack_error_with_its_code_only() -> None:
    with pytest.raises(SlackError) as raised:
        client(MockSlackTransport(error="msg_too_long")).post_digest([], "t")

    assert str(raised.value) == "msg_too_long"


def test_a_free_text_error_from_the_service_becomes_unknown() -> None:
    with pytest.raises(SlackError) as raised:
        client(
            MockSlackTransport(error="The channel #secret-room was archived by Dana")
        ).post_digest([], "t")

    assert str(raised.value) == "unknown" and "Dana" not in repr(raised.value)


class Scripted:
    def __init__(self, *answers: TransportResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    def request(self, method: str, path: str, **kwargs: Any) -> TransportResponse:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def upload(self, url: str, content: bytes) -> TransportResponse:
        return TransportResponse(200)


def test_a_429_waits_for_the_services_delay_up_to_three_times() -> None:
    sleeps: list[float] = []
    limited = TransportResponse(429, None, {"Retry-After": "7"})
    ok = TransportResponse(200, {"ok": True, "ts": "1.0"})
    transport = Scripted(limited, limited, limited, ok)

    assert client(transport, sleeps).post_digest([], "t") == "1.0"

    assert [s for s in sleeps if s > 0] == [7.0, 7.0, 7.0]
    with pytest.raises(SlackError, match="http_429"):
        client(Scripted(limited)).post_digest([], "t")


def test_a_network_error_is_retried_then_reported_without_text() -> None:
    transport = Scripted(TransportError("network_error"))

    with pytest.raises(SlackError, match="network_error"):
        client(transport).upload_file("a.pdf", b"%PDF", "t", "1.0")

    assert transport.calls == 3


# --- the real transport, over httpx's own mock -----------------------------------------------


def test_the_token_is_in_the_authorization_header_only_and_the_upload_gets_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1.0"})

    transport = HttpSlackTransport(
        TOKEN,
        client=httpx.Client(
            base_url="https://slack.com/api", transport=httpx.MockTransport(handler)
        ),
    )

    client(transport).post_digest([], "t")

    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(seen[0].url) and TOKEN not in seen[0].content.decode()
    assert TOKEN not in json.dumps([r.getMessage() for r in caplog.records])


def test_a_network_failure_on_the_upload_leaves_the_url_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(url: str, **kwargs: Any) -> None:
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(httpx, "post", refuse)
    transport = HttpSlackTransport(TOKEN)

    with pytest.raises(TransportError) as raised:
        transport.upload("https://files.slack.com/upload/v1/CAPSENTINEL", b"x")

    assert "CAPSENTINEL" not in str(raised.value) and "files.slack.com" not in repr(raised.value)


def test_a_post_is_never_sent_twice_after_a_server_error_or_a_lost_connection() -> None:
    for failure in (TransportResponse(503), TransportError("network_error")):
        transport = Scripted(failure)

        with pytest.raises(SlackError):
            client(transport).post_digest([], "digest")

        assert transport.calls == 1


def test_a_read_only_style_call_is_still_retried_after_a_server_error() -> None:
    transport = Scripted(
        TransportResponse(503),
        TransportResponse(200, {"ok": True, "upload_url": "https://x.invalid/u", "file_id": "F1"}),
        TransportResponse(200, {"ok": True}),
    )

    assert client(transport).upload_file("a.pdf", b"%PDF", "t", "1.0") == "F1"
    assert transport.calls == 3

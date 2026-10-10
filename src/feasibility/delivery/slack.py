"""Slack: one digest per run in the channel, and each pro-forma's PDF in the digest's thread.

The digest is built from the brief and from nothing else (`digest_blocks`). Every string that came
from data is escaped for Slack's markup (`&`, `<`, `>`), and every markup text object says
`verbatim: true`, so Slack parses no link, mention or channel name in it: a street such as
`100 ELM ST #ANNOUNCEMENTS` or a feed value such as `<!channel>` prints as the characters it is.

It uses a bot token (scopes `chat:write` and `files:write`), not an incoming webhook: a webhook
cannot upload files and carries its secret in a URL, and URLs end up in logs. The token travels
in the `Authorization` header only. The upload URL Slack hands back is a capability: it is used
once and never logged, stored or returned. Slack's request shapes here were written from its
documentation of the external upload flow and were not exercised against a workspace in this
build.
"""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from feasibility.config import DeliveryMode, Settings
from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.delivery.errors import (
    DeliveryConfigError,
    SlackError,
    SlackOutcomeUnknownError,
    TransportError,
    safe_code,
)
from feasibility.delivery.outbox import Outbox
from feasibility.delivery.transport import (
    Pacer,
    RecordedRequest,
    TransportResponse,
    send_with_retries,
)
from feasibility.llm import figures

BASE_URL = "https://slack.com/api"
MIN_INTERVAL_S = 1.1
MAX_BLOCKS = 50
MAX_TEXT = 3000
DOT = chr(0xB7)
# Error codes on which Slack may already have done what was asked.
AMBIGUOUS_ERRORS = frozenset(
    {"internal_error", "fatal_error", "request_timeout", "service_unavailable"}
)
CONFIG_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "missing_scope",
        "not_in_channel",
        "channel_not_found",
        "is_archived",
    }
)


def escape(text: str) -> str:
    """Slack's own escaping: the three characters that start its markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text[:MAX_TEXT], "verbatim": True}


def _money(value: str | None) -> str:
    return "not available" if value is None else figures.money(Decimal(value))


def _candidate_text(entry: BriefCandidate) -> str:
    f = entry.figures
    risks = sum(1 for s in entry.signals.items if s.polarity == "risk")
    opportunities = len(entry.signals.items) - risks
    lines = [
        f"*#{entry.rank} {escape(entry.street)}, {escape(entry.zip5 or '')}*",
        f"List price {_money(entry.list_price)} {DOT} ARV {_money(f.arv)}",
        f"Profit {_money(f.profit)} {DOT} margin {figures.percent(Decimal(f.margin))} {DOT} "
        f"max offer {_money(f.max_offer)}",
        f"Signals: {risks} risk, {opportunities} opportunity",
    ]
    narrative = entry.narrative
    if narrative.status == "accepted" and narrative.summary:
        lines.append(escape(narrative.summary))
    else:
        lines.append(f"_{escape(narrative.note)}_")
    return "\n".join(lines)


def digest_blocks(brief: Brief) -> tuple[list[dict[str, Any]], str]:
    """The digest as Slack blocks and the plain fallback text. Pure."""
    heading = f"Acquisition brief {DOT} {brief.market.title()} {DOT} {brief.as_of.isoformat()}"
    notes = []
    if brief.data_mode == "mock":
        notes.append("Synthetic demo data")
    if brief.completeness == "partial":
        notes.append("Partial: a later stage did not finish")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading}},
    ]
    if notes:
        blocks.append({"type": "context", "elements": [_mrkdwn(f" {DOT} ".join(notes))]})
    for entry in brief.candidates:
        blocks.append({"type": "section", "text": _mrkdwn(_candidate_text(entry))})
    skipped = brief.not_shown
    left = skipped.no_arv + skipped.unsizable + skipped.over_the_cap + skipped.no_pro_forma
    if left:
        blocks.append(
            {
                "type": "section",
                "text": _mrkdwn(
                    f"{left} more ranked candidates are not shown: {skipped.no_arv} with no value "
                    f"estimate yet, {skipped.unsizable} unsizable, {skipped.over_the_cap} over "
                    f"the cap, {skipped.no_pro_forma} with no pro-forma."
                ),
            }
        )
    blocks.append({"type": "context", "elements": [_mrkdwn(escape(brief.footer))]})
    return blocks[:MAX_BLOCKS], heading


def file_name(entry: BriefCandidate) -> str:
    """Built from the rank and the id only, never from an address."""
    return f"pro-forma-rank-{entry.rank}-candidate-{entry.candidate_id}.pdf"


def file_title(entry: BriefCandidate) -> str:
    return f"Pro-forma rank {entry.rank}"


class SlackTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse: ...

    def upload(self, url: str, content: bytes) -> TransportResponse: ...


UPLOAD_HOSTS = ("files.slack.com",)


def _is_slack_upload_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in UPLOAD_HOSTS and parts.port in (None, 443)


class SlackClient:
    def __init__(
        self,
        transport: SlackTransport,
        channel_id: str,
        *,
        pacer: Pacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._channel_id = channel_id
        self._pacer = pacer or Pacer(MIN_INTERVAL_S)
        self._sleep = sleep

    def _call(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        repeatable: bool = True,
    ) -> dict[str, Any]:
        """One request, retried as `send_with_retries` says."""
        response = send_with_retries(
            lambda: self._transport.request("POST", path, json=json, data=data),
            pacer=self._pacer,
            sleep=self._sleep,
            repeatable=repeatable,
            fail=SlackError,
            unknown=SlackOutcomeUnknownError,
        )
        return self._checked(response, repeatable)

    @staticmethod
    def _checked(response: TransportResponse, repeatable: bool) -> dict[str, Any]:
        body = response.body or {}
        if response.status == 200 and body.get("ok") is True:
            return body
        code = safe_code(body.get("error")) if response.status == 200 else f"http_{response.status}"
        if code in CONFIG_ERRORS:
            raise DeliveryConfigError(code)
        # Slack says of these that part of the operation may have happened before the error, and
        # an answer that is not JSON says nothing at all: for a request that posts something that
        # is an unknown outcome, never a refusal that is safe to try again.
        if not repeatable and (code in AMBIGUOUS_ERRORS or (response.status == 200 and not body)):
            raise SlackOutcomeUnknownError(code)
        raise SlackError(code)

    def post_digest(self, blocks: list[dict[str, Any]], text: str) -> str:
        """Post the digest to the channel; returns its `ts`. Links and media do not unfurl."""
        body = self._call(
            "/chat.postMessage",
            json={
                "channel": self._channel_id,
                "text": text,
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
                "link_names": False,
                "parse": "none",
            },
            repeatable=False,
        )
        ts = body.get("ts")
        if not isinstance(ts, str):
            # The message was posted; without its ts nothing can be threaded under it.
            raise SlackOutcomeUnknownError("no_ts")
        return ts

    def upload_file(self, name: str, content: bytes, title: str, thread_ts: str) -> str:
        """Upload a file into the digest's thread: ask for an upload URL, send the bytes, then
        complete the upload. The URL is used here and nowhere else."""
        started = self._call(
            "/files.getUploadURLExternal", data={"filename": name, "length": len(content)}
        )
        url, file_id = started.get("upload_url"), started.get("file_id")
        if not isinstance(url, str) or not isinstance(file_id, str):
            raise SlackError("no_upload_url")
        if not _is_slack_upload_url(url):
            # The bytes of a pro-forma go only to Slack's own upload host, over TLS.
            raise SlackError("bad_upload_url")
        self._pacer.wait()
        try:
            sent = self._transport.upload(url, content)
        except TransportError:
            raise SlackError("network_error") from None
        if sent.status != 200:
            raise SlackError(f"http_{sent.status}")
        self._call(
            "/files.completeUploadExternal",
            json={
                "files": [{"id": file_id, "title": title}],
                "channel_id": self._channel_id,
                "thread_ts": thread_ts,
            },
            repeatable=False,
        )
        return file_id


class HttpSlackTransport:
    """The real API. The token is in the `Authorization` header only."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        upload_client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL, timeout=httpx.Timeout(15.0, connect=5.0)
        )
        # A client of its own for the upload URL: it has no base URL and no header, so the token
        # cannot reach it, and it follows no redirect.
        self._upload_client = upload_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        try:
            response = self._client.request(
                method, path, json=json, data=data, headers=self._headers
            )
        except httpx.HTTPError:
            raise TransportError("network_error") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return TransportResponse(
            response.status_code, body if isinstance(body, dict) else None, dict(response.headers)
        )

    def upload(self, url: str, content: bytes) -> TransportResponse:
        """Send the bytes to the one-time upload URL. No token is sent to it: the URL is the
        credential, and it is never logged."""
        try:
            response = self._upload_client.post(url, content=content)
        except httpx.HTTPError:
            raise TransportError("network_error") from None
        return TransportResponse(response.status_code, None, {})


class MockSlackTransport:
    """Answers like Slack from memory: deterministic ids, every request kept. The upload URL is
    a made-up address that is never requested, and it is not kept."""

    def __init__(self, *, error: str | None = None, outbox: Outbox | None = None) -> None:
        self._outbox = outbox
        self._file_name = ""
        self.requests: list[RecordedRequest] = []
        self.posted: list[dict[str, Any]] = []
        self.uploads: list[bytes] = []
        self._error = error
        self._posts = 0
        self._files = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        self.requests.append(RecordedRequest(method, path, json or data))
        if self._outbox is not None:
            self._outbox.record("slack", method, path, json or data)
        if path == "/files.getUploadURLExternal":
            self._file_name = str((data or {}).get("filename", ""))
        if self._error is not None:
            return TransportResponse(200, {"ok": False, "error": self._error})
        if path == "/chat.postMessage":
            self._posts += 1
            self.posted.append(json or {})
            return TransportResponse(200, {"ok": True, "ts": f"mock-ts-{self._posts}"})
        if path == "/files.getUploadURLExternal":
            self._files += 1
            return TransportResponse(
                200,
                {
                    "ok": True,
                    "upload_url": f"https://files.slack.com/upload/v1/mock-{self._files}",
                    "file_id": f"mock-file-{self._files}",
                },
            )
        if path == "/files.completeUploadExternal":
            return TransportResponse(200, {"ok": True})
        return TransportResponse(404, {"ok": False, "error": "unknown_method"})

    def upload(self, url: str, content: bytes) -> TransportResponse:
        self.requests.append(RecordedRequest("POST", "upload", None, len(content)))
        self.uploads.append(content)
        if self._outbox is not None:
            self._outbox.record(
                "slack", "POST", "upload", {"file": self._file_name, "bytes": len(content)}
            )
            self._outbox.save_file(self._file_name, content)
        return TransportResponse(200, None, {})


def build_slack_transport(settings: Settings, outbox: Outbox | None = None) -> SlackTransport:
    """The mock transport, or the real one when delivery is live (which needs its token)."""
    if settings.delivery_mode is DeliveryMode.LIVE and settings.slack_bot_token is not None:
        return HttpSlackTransport(settings.slack_bot_token.get_secret_value())
    return MockSlackTransport(outbox=outbox)

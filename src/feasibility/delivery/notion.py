"""Notion: one row per candidate in the builder's pipeline database, updated each day it is briefed.

The row is built from the brief and from nothing else (`row_properties`), as plain text and
numbers: no link, no mention, no equation, never the narrative's risks (only an accepted summary),
never a comparable sale's address or a quote from the listing. The app writes only the properties
it owns; the builder's own `Decision` column is never written. `setup_schema` adds missing owned
properties and never deletes or renames one.

API version: `Notion-Version: 2022-06-28`. Notion's 2025 move to data sources changes the query and
create endpoints; this client uses the database endpoints of the version it pins, and that choice is
recorded in docs/ARCHITECTURE.md. Not checked against Notion's current reference in this build.
"""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from feasibility.config import DeliveryMode, Settings
from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.delivery.errors import (
    DeliveryConfigError,
    NotionError,
    NotionOutcomeUnknownError,
    TransportError,
)
from feasibility.delivery.transport import (
    Pacer,
    RecordedRequest,
    Transport,
    TransportResponse,
    send_with_retries,
)

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com"
KEY_PROPERTY = "Candidate key"
DECISION_PROPERTY = "Decision"
SUMMARY_LIMIT = 2000
MIN_INTERVAL_S = 0.35

# The properties the app owns: name -> (Notion type, number format or None). `Decision` is the
# builder's own column; setup creates it with its options and nothing ever writes it.
OWNED_PROPERTIES: dict[str, tuple[str, str | None]] = {
    "Name": ("title", None),
    KEY_PROPERTY: ("rich_text", None),
    "Brief date": ("date", None),
    "Rank": ("number", "number"),
    "Score": ("number", "number"),
    "Comps used": ("number", "number"),
    "List price": ("number", "dollar"),
    "ARV": ("number", "dollar"),
    "Total cost": ("number", "dollar"),
    "Profit": ("number", "dollar"),
    "Max offer": ("number", "dollar"),
    "Headroom": ("number", "dollar"),
    "Median $/sq ft": ("number", "dollar"),
    "Margin": ("number", "percent"),
    "Flags": ("multi_select", None),
    "Signals": ("multi_select", None),
    "Narrative": ("select", None),
    "Summary": ("rich_text", None),
    "Data": ("select", None),
}
DECISION_OPTIONS = ("Reviewing", "Pass", "Offer")
NARRATIVE_NAMES = {"accepted": "accepted", "withheld": "withheld", "not_available": "not available"}


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": value}}]


def _number(value: str | None) -> dict[str, Any]:
    return {"number": None if value is None else float(Decimal(value))}


def candidate_key(brief: Brief, entry: BriefCandidate) -> str:
    return f"{brief.market}:{entry.candidate_id}"


def row_properties(brief: Brief, entry: BriefCandidate) -> dict[str, Any]:
    """The properties of one candidate's row: only the ones the app owns, never `Decision`.

    Numbers are Notion numbers (floats); the exact decimal strings are in the brief and the PDF.
    `Margin` is the stored fraction (0.0757 shows as 7.57% in a percent-formatted column)."""
    f = entry.figures
    summary = entry.narrative.summary if entry.narrative.status == "accepted" else None
    return {
        "Name": {"title": _text(f"{entry.street}, {entry.zip5 or ''}".rstrip(", "))},
        KEY_PROPERTY: {"rich_text": _text(candidate_key(brief, entry))},
        "Brief date": {"date": {"start": brief.as_of.isoformat()}},
        "Rank": {"number": entry.rank},
        "Score": _number(entry.score),
        "Comps used": {"number": entry.comps.count_used},
        "List price": _number(entry.list_price),
        "ARV": _number(f.arv),
        "Total cost": _number(f.total_cost),
        "Profit": _number(f.profit),
        "Max offer": _number(f.max_offer),
        "Headroom": _number(f.headroom_vs_offer),
        "Median $/sq ft": _number(entry.comps.median_psf),
        "Margin": _number(f.margin),
        "Flags": {"multi_select": [{"name": flag.code} for flag in entry.flags]},
        "Signals": {"multi_select": [{"name": s.code} for s in entry.signals.items]},
        "Narrative": {"select": {"name": NARRATIVE_NAMES[entry.narrative.status]}},
        "Summary": {"rich_text": _text((summary or "")[:SUMMARY_LIMIT]) if summary else []},
        "Data": {"select": {"name": "synthetic" if brief.data_mode == "mock" else "live"}},
    }


def schema_properties() -> dict[str, Any]:
    """The database schema body for the properties the app owns (and `Decision`)."""
    body: dict[str, Any] = {}
    for name, (kind, number_format) in OWNED_PROPERTIES.items():
        if name == "Name":
            continue  # the title property exists in every database
        body[name] = {kind: {"format": number_format} if kind == "number" else {}}
    body[DECISION_PROPERTY] = {"select": {"options": [{"name": name} for name in DECISION_OPTIONS]}}
    return body


class NotionClient:
    """Serial, paced, retrying what is safe to retry; every failure is reduced to a code."""

    def __init__(
        self,
        transport: Transport,
        database_id: str,
        *,
        pacer: Pacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._database_id = database_id
        self._pacer = pacer or Pacer(MIN_INTERVAL_S)
        self._sleep = sleep

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        repeatable: bool = True,
    ) -> dict[str, Any]:
        """One request, retried as `send_with_retries` says."""
        response = send_with_retries(
            lambda: self._transport.request(method, path, json=body),
            pacer=self._pacer,
            sleep=self._sleep,
            repeatable=repeatable,
            fail=NotionError,
            unknown=NotionOutcomeUnknownError,
        )
        return self._checked(response, path)

    def _checked(self, response: TransportResponse, path: str) -> dict[str, Any]:
        if 200 <= response.status < 300:
            return response.body or {}
        # A service's own message and code text are not kept: the status says what happened.
        if response.status in (401, 403) or (
            response.status == 404 and path.startswith(f"/v1/databases/{self._database_id}")
        ):
            raise DeliveryConfigError(f"http_{response.status}")
        if response.status == 400:
            raise NotionError("validation_error")
        raise NotionError(f"http_{response.status}")

    def check_schema(self) -> dict[str, list[str]]:
        """Which owned properties are missing, or have another type, in the database."""
        found = self._call("GET", f"/v1/databases/{self._database_id}").get("properties", {})
        wanted = {**OWNED_PROPERTIES, DECISION_PROPERTY: ("select", None)}
        missing = [name for name in wanted if name not in found]
        wrong = [
            name
            for name, (kind, _) in wanted.items()
            if name in found and found[name].get("type") != kind
        ]
        return {"missing": missing, "wrong_type": wrong}

    def setup_schema(self) -> list[str]:
        """Add the missing owned properties (never remove or rename one); return the additions."""
        missing = self.check_schema()["missing"]
        additions = {name: spec for name, spec in schema_properties().items() if name in missing}
        if additions:
            self._call("PATCH", f"/v1/databases/{self._database_id}", {"properties": additions})
        return sorted(additions)

    def find_row(self, key: str) -> str | None:
        found = self._call(
            "POST",
            f"/v1/databases/{self._database_id}/query",
            {
                "filter": {"property": KEY_PROPERTY, "rich_text": {"equals": key}},
                "page_size": 1,
            },
        )
        results = found.get("results", [])
        page_id = results[0].get("id") if results else None
        return page_id if isinstance(page_id, str) else None

    def create_row(self, properties: dict[str, Any]) -> str:
        created = self._call(
            "POST",
            "/v1/pages",
            {"parent": {"database_id": self._database_id}, "properties": properties},
            repeatable=False,
        )
        page_id = created.get("id")
        if not isinstance(page_id, str):
            # The page may exist; without its id the row cannot be found by the update.
            raise NotionOutcomeUnknownError("no_page_id")
        return page_id

    def update_row(self, page_id: str, properties: dict[str, Any]) -> None:
        self._call("PATCH", f"/v1/pages/{page_id}", {"properties": properties})

    def upsert_row(self, brief: Brief, entry: BriefCandidate) -> tuple[str, str]:
        """Update the candidate's row, or create it. Returns the page id and what was done."""
        properties = row_properties(brief, entry)
        existing = self.find_row(candidate_key(brief, entry))
        if existing is not None:
            self.update_row(existing, properties)
            return existing, "updated"
        return self.create_row(properties), "created"


class HttpNotionTransport:
    """The real API. The token is in the `Authorization` header and nowhere else; nothing here
    logs a URL, a body or a response."""

    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL, timeout=httpx.Timeout(15.0, connect=5.0)
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        try:
            response = self._client.request(method, path, json=json, headers=self._headers)
        except httpx.HTTPError:
            raise TransportError("network_error") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return TransportResponse(
            response.status_code,
            body if isinstance(body, dict) else None,
            dict(response.headers),
        )


class MockNotionTransport:
    """Answers like Notion from memory: deterministic ids, every request kept.

    It is stateless across processes: a PATCH to any `mock-page-*` id succeeds, and the delivery
    ledger carries continuity between runs."""

    def __init__(self, *, missing: tuple[str, ...] = (), wrong_type: tuple[str, ...] = ()) -> None:
        self.requests: list[RecordedRequest] = []
        self.pages: dict[str, dict[str, Any]] = {}
        self._missing = set(missing)
        self._wrong = set(wrong_type)
        self._counter = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        self.requests.append(RecordedRequest(method, path, json))
        if method == "GET" and path.startswith("/v1/databases/"):
            return TransportResponse(200, {"object": "database", "properties": self._schema()})
        if method == "PATCH" and path.startswith("/v1/databases/"):
            for name in (json or {}).get("properties", {}):
                self._missing.discard(name)
            return TransportResponse(200, {"object": "database"})
        if method == "POST" and path.endswith("/query"):
            key = ((json or {}).get("filter") or {}).get("rich_text", {}).get("equals")
            hits = [
                {"object": "page", "id": page_id}
                for page_id, props in self.pages.items()
                if _key_of(props) == key
            ]
            return TransportResponse(200, {"object": "list", "results": hits[:1]})
        if method == "POST" and path == "/v1/pages":
            self._counter += 1
            page_id = f"mock-page-{self._counter}"
            self.pages[page_id] = (json or {}).get("properties", {})
            return TransportResponse(200, {"object": "page", "id": page_id})
        if method == "PATCH" and path.startswith("/v1/pages/mock-page-"):
            page_id = path.rsplit("/", 1)[1]
            self.pages[page_id] = {
                **self.pages.get(page_id, {}),
                **(json or {}).get("properties", {}),
            }
            return TransportResponse(200, {"object": "page", "id": page_id})
        return TransportResponse(404, {"object": "error", "code": "object_not_found"})

    def _schema(self) -> dict[str, Any]:
        found = {}
        for name, (kind, _) in {**OWNED_PROPERTIES, DECISION_PROPERTY: ("select", None)}.items():
            if name in self._missing:
                continue
            found[name] = {"type": "rich_text" if name in self._wrong else kind}
        return found


def _key_of(properties: dict[str, Any]) -> str | None:
    rich = properties.get(KEY_PROPERTY, {}).get("rich_text", [])
    return rich[0]["text"]["content"] if rich else None


def build_notion_transport(settings: Settings) -> Transport:
    """The mock transport, or the real one when delivery is live (which needs its token)."""
    if settings.delivery_mode is DeliveryMode.LIVE and settings.notion_token is not None:
        return HttpNotionTransport(settings.notion_token.get_secret_value())
    return MockNotionTransport()

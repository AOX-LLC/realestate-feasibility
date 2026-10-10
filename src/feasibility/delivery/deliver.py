"""Delivering a run's brief to Notion and Slack, once, and saying exactly what happened.

Order: build the brief from the database now (never from the stored copy), render the PDFs, write
the rows to Notion, then post the Slack digest and put each PDF in its thread. Every outbound call
is bracketed by the ledger (`sending` before, the outcome after, no transaction open in between),
and a lock keeps a second delivery of the same run out.

The two services are treated differently on purpose. Notion is at-least-once: a row is found or
created by its key and then updated, so a call that may have been repeated does no harm. Slack is
at-most-once: a post whose outcome is unknown is never repeated by this code, because a duplicate
digest is worse than a late one; a person looks and says so with `--resend slack`.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from feasibility.config import DeliveryMode, Settings
from feasibility.delivery import ledger
from feasibility.delivery.brief import Brief, BriefCandidate
from feasibility.delivery.build import BriefError, build_and_store, build_brief_snapshot
from feasibility.delivery.document import proforma_document
from feasibility.delivery.errors import (
    DeliveryBusyError,
    DeliveryConfigError,
    DeliveryError,
    DeliveryIncompleteError,
    DeliveryUnknownOutcomeError,
    NotionError,
    OutcomeUnknownError,
)
from feasibility.delivery.notion import (
    NotionClient,
    build_notion_transport,
    candidate_key,
    row_properties,
)
from feasibility.delivery.outbox import Outbox
from feasibility.delivery.pdf import render_pdf
from feasibility.delivery.slack import (
    SlackClient,
    build_slack_transport,
    digest_blocks,
    file_name,
    file_title,
)
from feasibility.delivery.transport import Pacer
from feasibility.proforma import store as proforma_store
from feasibility.proforma.model import ProformaResult

MOCK_DATABASE_ID = "0" * 32
MOCK_CHANNEL_ID = "C0MOCKCHAN"
RESENDABLE = frozenset({"slack"})
TARGETS = ("notion", "slack")


@dataclass(frozen=True)
class Clients:
    notion: NotionClient | None
    slack: SlackClient | None


@dataclass(frozen=True)
class ItemResult:
    """What happened to one item in this delivery. `skipped` means nothing was sent because it
    was already there; `superseded` means a later day's figures are on the page; `not_attempted`
    means a configuration error stopped the target first."""

    target: str
    item: str
    status: str
    error_code: str | None = None
    remote_ref: str | None = None


@dataclass
class DeliveryReport:
    run_id: int
    mode: str
    dry_run: bool
    items: list[ItemResult] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for item in self.items:
            found[item.status] = found.get(item.status, 0) + 1
        return found


def outbox_for(settings: Settings, brief: Brief) -> Outbox | None:
    """Mock delivery leaves what it would have sent in MEDIA_OUT, when that is configured."""
    if settings.delivery_mode is DeliveryMode.LIVE or settings.media_out is None:
        return None
    return Outbox(settings.media_out / f"{brief.market}-{brief.as_of.isoformat()}")


def build_clients(settings: Settings, outbox: Outbox | None = None) -> Clients:
    """The clients for the enabled targets: over mock transports, or live ones with the tokens."""
    notion = slack = None
    # A mock answers at once; only a live service is paced to its rate limit.
    pacer = Pacer(0.0) if settings.delivery_mode is DeliveryMode.MOCK else None
    if "notion" in settings.targets:
        notion = NotionClient(
            build_notion_transport(settings, outbox),
            settings.notion_database_id or MOCK_DATABASE_ID,
            pacer=pacer,
        )
    if "slack" in settings.targets:
        slack = SlackClient(
            build_slack_transport(settings, outbox),
            settings.slack_channel_id or MOCK_CHANNEL_ID,
            pacer=pacer,
        )
    return Clients(notion=notion, slack=slack)


def digest_of(payload: Any) -> str:
    """The sha256 of a payload as canonical JSON: what the ledger remembers of what was sent."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def deliver_brief(
    engine: Engine,
    settings: Settings,
    run_id: int,
    *,
    clients: Clients | None = None,
    dry_run: bool = False,
    resend: Iterable[str] = (),
    only: str | None = None,
    out: Path | None = None,
) -> DeliveryReport:
    """Send the run's brief to the enabled targets.

    Raises `DeliveryUnknownOutcomeError` (a Slack item may have been sent; permanent),
    `DeliveryConfigError` (credentials, channel or database wrong; permanent),
    `DeliveryIncompleteError` (something failed that another try may fix) or `DeliveryBusyError`
    (another delivery of this run is going; nothing was sent), each after doing everything else it
    could. The report is `error.report` on the first three. A dry run builds and renders and sends
    nothing, writes no ledger row and takes no lock."""
    resend = frozenset(resend)
    if resend - RESENDABLE:
        raise ValueError(f"only {', '.join(sorted(RESENDABLE))} can be sent again")
    if only is not None and only not in TARGETS:
        raise ValueError(f"only must be one of {', '.join(TARGETS)}")
    targets = [name for name in settings.targets if only in (None, name)]
    mode = settings.delivery_mode.value
    report = DeliveryReport(run_id=run_id, mode=mode, dry_run=dry_run)

    if dry_run:
        brief = build_brief_snapshot(engine, run_id)
        _dry_run(engine, settings, brief, targets, out, report)
        return report

    with ledger.run_lock(engine, run_id):
        # Built now from the database: the stored row is a copy for audit and is never sent.
        brief, _ = build_and_store(engine, run_id)
        clients = clients or build_clients(settings, outbox_for(settings, brief))
        for name in resend & set(targets):
            ledger.drop(engine, run_id, name, mode)
        problems = _Problems()
        if "notion" in targets:
            _deliver_notion(engine, report, brief, clients.notion, mode, problems)
        if "slack" in targets:
            documents = _documents_to_send(engine, brief, mode) if clients.slack else {}
            _deliver_slack(engine, report, brief, clients.slack, mode, documents, problems)
    problems.raise_for(report)
    return report


# --- what a delivery has gone wrong with ---------------------------------------------------------


@dataclass
class _Problems:
    config: DeliveryConfigError | None = None
    stopped: set[str] = field(default_factory=set)  # targets a configuration error has stopped
    unknown_slack: bool = False
    incomplete: bool = False

    def raise_for(self, report: DeliveryReport) -> None:
        error: DeliveryError
        if self.unknown_slack:
            error = DeliveryUnknownOutcomeError("slack_unknown")
        elif self.config is not None:
            error = self.config
        elif self.incomplete:
            error = DeliveryIncompleteError("incomplete")
        else:
            return
        error.report = report
        raise error


def _end(
    engine: Engine,
    run_id: int,
    target: str,
    item: str,
    mode: str,
    attempt: Callable[[], str],
    report: DeliveryReport,
    problems: _Problems,
) -> str | None:
    """Make one bracketed call. `attempt` returns the remote reference. Returns it on success;
    on any failure records the right ledger status and report line and returns None (a
    configuration error also stops the target, which the caller sees in `problems.config`)."""
    try:
        reference = attempt()
    except OutcomeUnknownError as error:
        ledger.finish(engine, run_id, target, item, mode, "unknown", error_code=error.code)
        report.items.append(ItemResult(target, item, "unknown", error.code))
        if target == "slack":
            problems.unknown_slack = True
        else:
            problems.incomplete = True  # Notion upserts, so the next try is safe
        return None
    except DeliveryConfigError as error:
        ledger.finish(engine, run_id, target, item, mode, "failed", error_code=error.code)
        report.items.append(ItemResult(target, item, "failed", error.code))
        problems.config = problems.config or error
        problems.stopped.add(target)
        return None
    except DeliveryError as error:
        ledger.finish(engine, run_id, target, item, mode, "failed", error_code=error.code)
        report.items.append(ItemResult(target, item, "failed", error.code))
        problems.incomplete = True
        return None
    ledger.finish(engine, run_id, target, item, mode, "sent", remote_ref=reference)
    report.items.append(ItemResult(target, item, "sent", None, reference))
    return reference


# --- Notion --------------------------------------------------------------------------------------


def _deliver_notion(
    engine: Engine,
    report: DeliveryReport,
    brief: Brief,
    client: NotionClient | None,
    mode: str,
    problems: _Problems,
) -> None:
    if client is None:
        problems.config = problems.config or DeliveryConfigError("notion_not_configured")
        return
    schema_checked: list[bool] = []  # becomes [True] once the schema has been read
    for position, entry in enumerate(brief.candidates):
        item = f"row:{entry.candidate_id}"
        properties = row_properties(brief, entry)
        sha = digest_of(properties)
        last = ledger.remembered(engine, "notion", item, mode)
        if last is not None and last.as_of > brief.as_of:
            # The page already carries a later day's figures; an older brief (a resend, a late
            # job) must not write them back.
            report.items.append(ItemResult("notion", item, "superseded", None, last.remote_ref))
            continue
        if last is not None and last.content_sha256 == sha:
            ledger.note_skipped(engine, report.run_id, "notion", item, mode, sha, last.remote_ref)
            report.items.append(ItemResult("notion", item, "skipped", None, last.remote_ref))
            continue

        known = last.remote_ref if last is not None else None
        write = partial(_write_row, client, brief, entry, known, schema_checked)
        ledger.start(engine, report.run_id, "notion", item, mode, sha)
        _end(engine, report.run_id, "notion", item, mode, write, report, problems)
        if "notion" in problems.stopped:
            for later in brief.candidates[position + 1 :]:
                report.items.append(
                    ItemResult("notion", f"row:{later.candidate_id}", "not_attempted")
                )
            return


def _write_row(
    client: NotionClient,
    brief: Brief,
    entry: BriefCandidate,
    known: str | None,
    schema_checked: list[bool],
) -> str:
    """The database's schema is read once, before the first row that needs a call."""
    if not schema_checked:
        _check_schema(client)
        schema_checked.append(True)
    return _upsert(client, brief, entry, known)


def _check_schema(client: NotionClient) -> None:
    """Refuse to write into a database that lacks a property the app owns or has it as another
    type: Notion would answer each row with a validation error, one at a time."""
    found = client.check_schema()
    if found["missing"] or found["wrong_type"]:
        raise DeliveryConfigError("notion_schema")


def _upsert(client: NotionClient, brief: Brief, entry: BriefCandidate, known: str | None) -> str:
    """Update the page this candidate's row was last written to, or find the row by its key and
    update it, or create it."""
    if known is not None:
        try:
            client.update_row(known, row_properties(brief, entry))
        except NotionError as error:
            # Gone (deleted, trashed) or archived: look the row up by its key instead.
            if error.code not in ("http_404", "validation_error"):
                raise
        else:
            return known
    page_id, _ = client.upsert_row(brief, entry)
    return page_id


# --- Slack ---------------------------------------------------------------------------------------


def _documents_to_send(engine: Engine, brief: Brief, mode: str) -> dict[int, bytes]:
    """The PDF of every candidate whose file has not gone to Slack yet, rendered before anything
    is posted: a document that cannot be made stops the delivery while nothing is half-sent."""
    documents: dict[int, bytes] = {}
    for entry in brief.candidates:
        state = ledger.read(engine, brief.run_id, "slack", f"file:{entry.candidate_id}", mode)
        if state is not None and state.status in ("sent", "unknown", "sending"):
            continue
        documents[entry.candidate_id] = _pdf_of(engine, brief, entry)
    return documents


def _pdf_of(engine: Engine, brief: Brief, entry: BriefCandidate) -> bytes:
    with engine.connect() as connection:
        stored = proforma_store.read_proforma(connection, brief.run_id, entry.candidate_id)
    if stored is None or stored.result is None:
        raise BriefError(f"candidate {entry.candidate_id} has no pro-forma to print")
    try:
        document = proforma_document(brief, entry, ProformaResult.model_validate(stored.result))
    except ValueError:
        raise BriefError(
            f"candidate {entry.candidate_id}: the pro-forma and the brief disagree"
        ) from None
    return render_pdf(document)


def _deliver_slack(
    engine: Engine,
    report: DeliveryReport,
    brief: Brief,
    client: SlackClient | None,
    mode: str,
    documents: dict[int, bytes],
    problems: _Problems,
) -> None:
    if client is None:
        problems.config = problems.config or DeliveryConfigError("slack_not_configured")
        return
    run_id = report.run_id
    blocks, text = digest_blocks(brief)
    state = ledger.read(engine, run_id, "slack", "digest", mode)
    thread: str | None = None
    if state is not None and state.status == "sent":
        report.items.append(ItemResult("slack", "digest", "skipped", None, state.remote_ref))
        thread = state.remote_ref
    elif state is not None and state.status == "sending":
        # Written a moment ago by a call that has not ended and holds no lock: too new to call.
        raise DeliveryBusyError("sending")
    elif state is not None and state.status == "unknown":
        report.items.append(ItemResult("slack", "digest", "unknown", state.error_code))
        problems.unknown_slack = True
    else:
        sha = digest_of({"text": text, "blocks": blocks})
        ledger.start(engine, run_id, "slack", "digest", mode, sha)
        thread = _end(
            engine,
            run_id,
            "slack",
            "digest",
            mode,
            lambda: client.post_digest(blocks, text),
            report,
            problems,
        )
    if thread is None:
        return  # no digest, no thread to put files in
    for entry in brief.candidates:
        _send_file(
            engine, report, client, mode, entry, documents.get(entry.candidate_id), thread, problems
        )
        if "slack" in problems.stopped:
            return


def _send_file(
    engine: Engine,
    report: DeliveryReport,
    client: SlackClient,
    mode: str,
    entry: BriefCandidate,
    pdf: bytes | None,
    thread: str,
    problems: _Problems,
) -> None:
    item = f"file:{entry.candidate_id}"
    state = ledger.read(engine, report.run_id, "slack", item, mode)
    if state is not None and state.status == "sent":
        report.items.append(ItemResult("slack", item, "skipped", None, state.remote_ref))
        return
    if state is not None and state.status in ("unknown", "sending"):
        report.items.append(ItemResult("slack", item, "unknown", state.error_code))
        problems.unknown_slack = True
        return
    if pdf is None:
        raise BriefError(f"candidate {entry.candidate_id}: its file was not rendered")
    ledger.start(engine, report.run_id, "slack", item, mode, hashlib.sha256(pdf).hexdigest())
    _end(
        engine,
        report.run_id,
        "slack",
        item,
        mode,
        lambda: client.upload_file(file_name(entry), pdf, file_title(entry), thread),
        report,
        problems,
    )


# --- dry run -------------------------------------------------------------------------------------


def _dry_run(
    engine: Engine,
    settings: Settings,
    brief: Brief,
    targets: list[str],
    out: Path | None,
    report: DeliveryReport,
) -> None:
    folder = out or settings.local_dir / "briefs" / brief.as_of.isoformat()
    if "notion" in targets:
        for entry in brief.candidates:
            report.payloads.append(
                {
                    "target": "notion",
                    "item": f"row:{entry.candidate_id}",
                    "key": candidate_key(brief, entry),
                    "properties": row_properties(brief, entry),
                }
            )
    if "slack" in targets:
        blocks, text = digest_blocks(brief)
        report.payloads.append(
            {"target": "slack", "item": "digest", "text": text, "blocks": blocks}
        )
        folder.mkdir(parents=True, exist_ok=True)
        for entry in brief.candidates:
            path = folder / file_name(entry)
            path.write_bytes(_pdf_of(engine, brief, entry))
            report.files.append(path)
            report.payloads.append(
                {
                    "target": "slack",
                    "item": f"file:{entry.candidate_id}",
                    "name": file_name(entry),
                    "title": file_title(entry),
                }
            )

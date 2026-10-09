"""Command-line entry point: `feasibility --help`."""

import asyncio
import dataclasses
import json
import signal
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError
from sqlalchemy import Connection

from feasibility.api.app import create_app
from feasibility.config import REPO_ROOT, get_settings
from feasibility.db import get_engine, upgrade_to_head
from feasibility.jobs.handlers import SourcingRunPayload, build_registry, enqueue_job
from feasibility.jobs.worker import Worker
from feasibility.logging import configure_logging
from feasibility.markets.loader import PackError, get_pack, load_pack, pack_paths
from feasibility.markets.schema import FileKind
from feasibility.proforma import render
from feasibility.proforma import store as proforma_store
from feasibility.snapshot.load import LiveModeSeedError
from feasibility.snapshot.load import seed as seed_snapshot
from feasibility.sources.base import ImportRequest
from feasibility.sources.cad_csv.importer import CadCsvParcelSource
from feasibility.sources.rentcast import verify
from feasibility.sourcing import store as sourcing_store
from feasibility.sourcing.errors import SourcingError
from feasibility.sourcing.run import resolve_run_date, run_sourcing

# Uncaught errors go through logging (and its secret redaction), not Typer's printer.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
market_app = typer.Typer(no_args_is_help=True, help="Market pack commands.")
app.add_typer(market_app, name="market")
source_app = typer.Typer(no_args_is_help=True, help="Daily sourcing: run it, read the result.")
app.add_typer(source_app, name="source")
proforma_app = typer.Typer(no_args_is_help=True, help="Pro-formas of a run (read-only).")
app.add_typer(proforma_app, name="proforma")
eval_app = typer.Typer(no_args_is_help=True, help="Model evals: replay by default, no live calls.")
app.add_typer(eval_app, name="eval")

TOP_CANDIDATES_SHOWN = 10
EVALS_DIR = REPO_ROOT / "evals"
SourcingStatus = Annotated[
    str, typer.Option("--status", help="ranked, filtered or unscored", show_default=True)
]


@app.callback()
def main() -> None:
    """Daily acquisition brief for a spec home builder."""
    configure_logging(get_settings().secret_values())


@app.command()
def migrate() -> None:
    """Upgrade the database schema to the latest revision."""
    upgrade_to_head(get_engine())
    typer.echo("schema at head")


@market_app.command("validate")
def validate_markets(
    paths: Annotated[list[Path] | None, typer.Argument(help="Pack files; default: all")] = None,
) -> None:
    """Validate market pack files."""
    failures = 0
    for path in paths or pack_paths():
        try:
            pack = load_pack(path)
        except (PackError, ValidationError) as error:
            failures += 1
            typer.echo(f"FAIL {path.name}: {error}", err=True)
            continue
        typer.echo(f"ok   {path.name}: {pack.market.name}")
    if failures:
        raise typer.Exit(code=1)


@app.command("import-cad")
def import_cad(
    archive: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    kind: Annotated[str, typer.Option(help="certified or current")] = "certified",
    roll_year: Annotated[int | None, typer.Option(help="Default: from the file name")] = None,
    file_date: Annotated[
        str | None, typer.Option(help="YYYY-MM-DD; default: the archive member's date")
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace a load with different contents")] = False,
    market: Annotated[str | None, typer.Option(help="Default: MARKET setting")] = None,
) -> None:
    """Import a county appraisal archive you downloaded (e.g. DCAD2026_CURRENT.ZIP)."""
    if kind not in ("certified", "current"):
        raise typer.BadParameter("kind must be 'certified' or 'current'")
    file_kind: FileKind = "certified" if kind == "certified" else "current"
    pack = get_pack(market or get_settings().market)
    request = ImportRequest(
        archive=archive,
        kind=file_kind,
        roll_year=roll_year,
        file_date=date.fromisoformat(file_date) if file_date else None,
        force=force,
    )
    report = CadCsvParcelSource(get_engine(), pack).import_archive(request)
    typer.echo(json.dumps(dataclasses.asdict(report), default=str, indent=2))


@app.command()
def enqueue(
    kind: str,
    payload: Annotated[str, typer.Option(help="JSON payload for the job kind")] = "{}",
    dedupe_key: Annotated[str | None, typer.Option(help="Skip if one is already active")] = None,
) -> None:
    """Queue a job (validated against its kind's payload model)."""
    with get_engine().begin() as connection:
        job_id = enqueue_job(
            connection, build_registry(), kind, json.loads(payload), dedupe_key=dedupe_key
        )
    typer.echo(f"queued job {job_id}" if job_id else "an identical job is already active")


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Run at most one job and exit")] = False,
) -> None:
    """Run the job worker."""
    job_worker = Worker(get_engine(), get_settings(), build_registry())
    if once:
        job_worker.run_once()
        return
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    job_worker.run_forever(stop)


@source_app.command("run")
def source_run(
    market: Annotated[str | None, typer.Option(help="Default: MARKET setting")] = None,
    as_of: Annotated[
        str | None, typer.Option(help="YYYY-MM-DD; required in mock mode, today in live mode")
    ] = None,
    enqueue_only: Annotated[
        bool, typer.Option("--enqueue", help="Queue the run for the worker instead of running it")
    ] = False,
) -> None:
    """Source one day: sync the feed, diff, match, filter, score and rank, then price the top."""
    settings = get_settings()
    market_id = market or settings.market
    try:
        requested = date.fromisoformat(as_of) if as_of else None
        # Resolve the date before queueing, so a bad one fails here and not in the worker,
        # and a job queued for "today" keeps the date it was queued for.
        run_date, _ = resolve_run_date(settings, get_pack(market_id), requested)
        if enqueue_only:
            _enqueue_sourcing(market_id, run_date)
            return
        result = run_sourcing(get_engine(), settings, market_id, run_date)
    except (SourcingError, PackError, ValueError) as error:
        typer.echo(f"sourcing refused: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"run {result.run_id} for {result.as_of}, sync {result.sync_status}")
    for name, value in result.counts.model_dump().items():
        typer.echo(f"{name} {value}")
    counts = result.counts
    typer.echo(
        f"estimates: {counts.estimates_called} called, {counts.estimates_reused} reused, "
        f"{counts.estimates_deferred} deferred, {counts.estimates_failed} failed"
    )
    with get_engine().connect() as connection:
        top = sourcing_store.candidate_summaries(
            connection, result.run_id, "ranked", TOP_CANDIDATES_SHOWN
        )
    typer.echo(f"top {len(top)} ranked:")
    for line in top:
        typer.echo(f"{line.rank} {line.score} {line.price} {line.address}")


def _enqueue_sourcing(market: str, as_of: date) -> None:
    payload = SourcingRunPayload(market=market, as_of=as_of)
    dedupe_key = f"sourcing.run:{market}:{as_of}"
    with get_engine().begin() as connection:
        job_id = enqueue_job(
            connection,
            build_registry(),
            "sourcing.run",
            payload.model_dump(mode="json"),
            dedupe_key=dedupe_key,
        )
    typer.echo(f"queued job {job_id}" if job_id else "an identical job is already active")


@source_app.command("show")
def source_show(
    market: Annotated[str | None, typer.Option(help="Default: MARKET setting")] = None,
    run_id: Annotated[int | None, typer.Option(help="Default: the latest completed run")] = None,
    status: SourcingStatus = "ranked",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
) -> None:
    """Print a run's candidates (read-only)."""
    if status not in ("ranked", "filtered", "unscored"):
        raise typer.BadParameter("status must be ranked, filtered or unscored")
    with get_engine().connect() as connection:
        shown_run = run_id or sourcing_store.latest_run_id(
            connection, market or get_settings().market
        )
        if shown_run is None:
            typer.echo("no completed run yet", err=True)
            raise typer.Exit(code=1)
        lines = sourcing_store.candidate_summaries(connection, shown_run, status, limit)
    typer.echo(f"run {shown_run}, {status}: {len(lines)} shown")
    for line in lines:
        typer.echo(
            "\t".join(
                [
                    str(line.rank or "-"),
                    str(line.score or "-"),
                    str(line.price),
                    line.change_kind,
                    line.detail or "-",
                    line.address,
                ]
            )
        )


def _proforma_run(connection: Connection, market: str | None, run_id: int | None) -> int:
    """The run to read: the given one, else the latest completed. Exit 2 when there is none."""
    if run_id is None:
        run_id = sourcing_store.latest_run_id(connection, market or get_settings().market)
        if run_id is None:
            typer.echo("no completed run yet", err=True)
            raise typer.Exit(code=2)
    elif not sourcing_store.run_exists(connection, run_id):
        typer.echo(f"no run {run_id}", err=True)
        raise typer.Exit(code=2)
    return run_id


@proforma_app.command("list")
def proforma_list(
    market: Annotated[str | None, typer.Option(help="Default: MARKET setting")] = None,
    run_id: Annotated[int | None, typer.Option(help="Default: the latest completed run")] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="computed, no_arv or unsizable")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
) -> None:
    """Print a run's pro-formas in rank order (read-only)."""
    if status not in (None, "computed", "no_arv", "unsizable"):
        raise typer.BadParameter("status must be computed, no_arv or unsizable")
    with get_engine().connect() as connection:
        shown_run = _proforma_run(connection, market, run_id)
        items = proforma_store.read_proformas(connection, shown_run, status=status, limit=limit)
    typer.echo(f"run {shown_run}, {status or 'all statuses'}: {len(items)} shown")
    for line in render.list_lines(items):
        typer.echo(line)


@proforma_app.command("show")
def proforma_show(
    candidate_id: Annotated[int, typer.Argument(min=1, help="The candidate's id")],
    market: Annotated[str | None, typer.Option(help="Default: MARKET setting")] = None,
    run_id: Annotated[int | None, typer.Option(help="Default: the latest completed run")] = None,
    sensitivity: Annotated[
        bool, typer.Option("--sensitivity", help="Also print the 60-cell sensitivity grid")
    ] = False,
) -> None:
    """Print one candidate's whole pro-forma, section by section (read-only)."""
    with get_engine().connect() as connection:
        shown_run = _proforma_run(connection, market, run_id)
        item = proforma_store.read_proforma(connection, shown_run, candidate_id)
    if item is None:
        typer.echo(f"candidate {candidate_id} has no pro-forma in run {shown_run}", err=True)
        raise typer.Exit(code=2)
    try:
        lines = render.show_lines(item, sensitivity=sensitivity)
    except ValidationError as error:
        # A result stored under an older version of the model; say so rather than trace.
        typer.echo(
            f"the stored result of candidate {candidate_id} does not fit the current model "
            f"({error.error_count()} problems)",
            err=True,
        )
        raise typer.Exit(code=1) from None
    typer.echo(f"run {shown_run}")
    for line in lines:
        typer.echo(line)


@app.command("verify-rentcast")
def verify_rentcast() -> None:
    """Check the live RentCast API against the models (at most 4 calls, from the budget)."""
    settings = get_settings()
    if not settings.is_live:
        typer.echo("verify-rentcast needs DATA_MODE=live and RENTCAST_API_KEY", err=True)
        raise typer.Exit(code=2)
    report_path = verify.verify(get_engine(), settings)
    typer.echo(f"field report written to {report_path}")


@app.command()
def seed() -> None:
    """Load the committed synthetic snapshot (idempotent; no network)."""
    try:
        report = seed_snapshot(get_engine(), get_settings())
    except LiveModeSeedError as error:
        # Exit cleanly so `migrate && seed` still brings a live stack up.
        typer.echo(f"seed skipped: {error}")
        return
    for kind, imported in (("certified", report.certified), ("current", report.current)):
        if imported is None:
            typer.echo(f"{kind}: not in the snapshot")
        else:
            typer.echo(
                f"{kind}: {imported.status}, {imported.rows_loaded} loaded of "
                f"{imported.rows_read} read, {imported.rows_skipped} skipped"
            )
    typer.echo(f"listings upserted: {report.listings}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 4501,
) -> None:
    """Run the read-only HTTP API."""
    settings = get_settings()
    uvicorn.run(
        create_app(settings, get_engine()),
        host=host,
        port=port,
        # Keep the root handler (and its secret redaction) for uvicorn's own loggers.
        log_config=None,
        server_header=False,
    )


@eval_app.command("signals")
def eval_signals(
    split: Annotated[str, typer.Option(help="dev, holdout or all")] = "dev",
    out: Annotated[Path | None, typer.Option(help="Write scorecard files here")] = None,
    max_usd: Annotated[float, typer.Option(min=0, help="Spend cap for the session")] = 1.0,
    min_precision: Annotated[float | None, typer.Option(min=0, max=1)] = None,
    min_recall: Annotated[float | None, typer.Option(min=0, max=1)] = None,
) -> None:
    """Run the extraction eval. Replay mode needs recordings: a missing one fails the run."""
    # Imported here so the other commands do not load the model library.
    from feasibility.evals import signals as signals_eval
    from feasibility.evals.client import build_eval_client, ended_message

    settings = get_settings()
    if settings.is_live:
        typer.echo(
            "evals use the committed synthetic records: run them with DATA_MODE=mock", err=True
        )
        raise typer.Exit(code=2)
    try:
        signals_eval.splits_for(split)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    session = build_eval_client(settings, Decimal(str(max_usd)))
    report = asyncio.run(
        signals_eval.run_signals_eval(
            session.client,
            session.mode,
            split,
            [settings.mls_dir / "dallas.json", EVALS_DIR / "signals" / "extra.json"],
            EVALS_DIR / "signals" / "answer_key.json",
        )
    )
    if report.ended_by is not None:
        typer.echo(ended_message(report.ended_by, report.ended_error), err=True)
        raise typer.Exit(code=1)
    problems: list[str] = []
    for split_report in report.splits:
        summary = split_report.summary
        typer.echo(signals_eval.render_summary_markdown(summary))
        if out is not None:
            for path in signals_eval.write_scorecard(split_report.scorecard, summary, out):
                typer.echo(f"wrote {path}")
        problems += [f"{summary.split}: {text}" for text in signals_eval.hard_failures(summary)]
        problems += [
            f"{summary.split}: {text}"
            for text in signals_eval.floor_problems(summary, min_precision, min_recall)
        ]
        if summary.cases_errored:
            problems.append(f"{summary.split}: {summary.cases_errored} cases errored")
    for problem in problems:
        typer.echo(problem, err=True)
    if problems:
        raise typer.Exit(code=1)


@eval_app.command("narrative")
def eval_narrative(
    out: Annotated[Path | None, typer.Option(help="Write scorecard files here")] = None,
    max_usd: Annotated[float, typer.Option(min=0, help="Spend cap for the session")] = 1.0,
    min_acceptance: Annotated[float | None, typer.Option(min=0, max=1)] = None,
) -> None:
    """Run the narrative eval. Replay mode needs recordings: a missing one fails the run."""
    from feasibility.evals import narrative as narrative_eval
    from feasibility.evals.client import build_eval_client, ended_message

    settings = get_settings()
    if settings.is_live:
        typer.echo(
            "evals use the committed synthetic cases: run them with DATA_MODE=mock", err=True
        )
        raise typer.Exit(code=2)
    session = build_eval_client(settings, Decimal(str(max_usd)))
    report = asyncio.run(
        narrative_eval.run_narrative_eval(
            session.client, session.mode, EVALS_DIR / "narrative" / "cases.json"
        )
    )
    if report.ended_by is not None:
        typer.echo(ended_message(report.ended_by, report.ended_error), err=True)
        raise typer.Exit(code=1)
    summary = report.summary
    typer.echo(narrative_eval.render_summary_markdown(summary))
    if out is not None:
        for path in narrative_eval.write_scorecard(report.scorecard, summary, out):
            typer.echo(f"wrote {path}")
    problems = narrative_eval.hard_failures(summary)
    problems += narrative_eval.floor_problems(summary, min_acceptance)
    if summary.cases_errored:
        problems.append(f"{summary.cases_errored} cases errored")
    for problem in problems:
        typer.echo(problem, err=True)
    if problems:
        raise typer.Exit(code=1)

"""Command-line entry point: `feasibility --help`."""

import dataclasses
import json
import signal
import threading
from datetime import date
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from pydantic import ValidationError

from feasibility.config import get_settings
from feasibility.db import get_engine, upgrade_to_head
from feasibility.jobs.handlers import build_registry, enqueue_job
from feasibility.jobs.worker import Worker
from feasibility.logging import configure_logging
from feasibility.markets.loader import PackError, get_pack, load_pack, pack_paths
from feasibility.markets.schema import FileKind
from feasibility.sources.base import ImportRequest
from feasibility.sources.cad_csv.importer import CadCsvParcelSource

app = typer.Typer(no_args_is_help=True, add_completion=False)
market_app = typer.Typer(no_args_is_help=True, help="Market pack commands.")
app.add_typer(market_app, name="market")


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

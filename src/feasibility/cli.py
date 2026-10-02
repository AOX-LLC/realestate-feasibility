"""Command-line entry point: `feasibility --help`."""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from feasibility.config import get_settings
from feasibility.db import get_engine, upgrade_to_head
from feasibility.logging import configure_logging
from feasibility.markets.loader import PackError, load_pack, pack_paths

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

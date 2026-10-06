"""Command-line entry point: `uv run app ...`."""

from datetime import UTC, datetime

import psycopg
import typer

from pipeline import __version__
from pipeline.db import connect, server_version
from pipeline.ingest.cli import ingest_app
from pipeline.match.cli import match_app, recompetes_app
from pipeline.migrate import current_revision, head_revision
from pipeline.settings import MissingSettingError, Settings, get_settings
from pipeline.status import build_report

app = typer.Typer(
    help="Federal contracts intelligence pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(ingest_app, name="ingest")
app.add_typer(match_app, name="match")
app.add_typer(recompetes_app, name="recompetes")


@app.command()
def version() -> None:
    """Print the pipeline version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check settings, the database connection and migrations. Never prints secret values."""
    settings = get_settings()
    typer.echo("Environment")
    for name, is_set in settings.env_status().items():
        typer.echo(f"  {name:<24}{'set' if is_set else 'missing'}")
    typer.echo("Database")
    if not _report_database(settings):
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Last run and errors per job, backfill progress, SAM.gov requests used, row counts."""
    settings = get_settings()
    try:
        database_url = settings.require_database_url()
    except MissingSettingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        with connect(database_url) as conn:
            lines = build_report(conn, settings.sam_daily_request_limit, datetime.now(UTC))
    except psycopg.errors.UndefinedTable as exc:
        typer.echo("The ingest tables don't exist yet. Run: uv run alembic upgrade head", err=True)
        raise typer.Exit(code=1) from exc
    except psycopg.OperationalError as exc:
        typer.echo(f"Can't reach the database ({exc}). Try: docker compose up -d db", err=True)
        raise typer.Exit(code=1) from exc
    for line in lines:
        typer.echo(line)


def _report_database(settings: Settings) -> bool:
    try:
        database_url = settings.require_database_url()
    except MissingSettingError as exc:
        typer.echo(f"  {exc}")
        return False
    try:
        pg_version = server_version(database_url)
        current = current_revision(database_url)
    except psycopg.OperationalError as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        typer.echo(f"  connection  failed: {reason}")
        typer.echo("  Is the database running? Try: docker compose up -d db")
        return False
    typer.echo(f"  connection  ok (PostgreSQL {pg_version})")
    head = head_revision()
    if current == head:
        typer.echo(f"  migrations  up to date ({current})")
        return True
    typer.echo(f"  migrations  at {current or 'none'}, latest is {head}")
    typer.echo("  Run: uv run alembic upgrade head")
    return False

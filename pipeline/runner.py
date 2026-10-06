"""Shared plumbing for CLI jobs: run one job inside `ingest_run` and report how it went."""

from collections.abc import Callable
from datetime import UTC, date, datetime

import typer

from pipeline.db import connect
from pipeline.ingest.context import IngestContext, ingest_run
from pipeline.ingest.runs import JobAlreadyRunning
from pipeline.settings import Settings


def log(message: str) -> None:
    typer.echo(f"[{datetime.now(UTC):%H:%M:%S}] {message}")


def today() -> date:
    return datetime.now(UTC).date()


def run_job(
    settings: Settings,
    database_url: str,
    source: str,
    job: str,
    params: dict[str, object],
    action: Callable[[IngestContext], None],
) -> None:
    """Run `action` as one logged, locked job run. A failure exits with code 1."""
    run_id = None
    try:
        with ingest_run(database_url, settings.raw_archive_path(), source, job, params, log) as ctx:
            run_id = ctx.run.id
            action(ctx)
    except JobAlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        typer.echo(f"{source} {job} failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    print_summary(database_url, run_id)


def print_summary(database_url: str, run_id: int | None) -> None:
    with connect(database_url) as conn:
        row = conn.execute(
            "SELECT status, rows_in, rows_upserted, requests_made FROM ingest_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        return
    status, rows_in, written, requests = row
    typer.echo(f"{status}: {rows_in} rows read, {written} written, {requests} requests.")
    if status == "partial":
        typer.echo("Stopped at the daily request budget. Run it again later to continue.")

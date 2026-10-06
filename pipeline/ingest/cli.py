"""`app ingest ...` commands."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import typer

from pipeline.db import connect
from pipeline.ingest.awards import SOURCE as AWARDS
from pipeline.ingest.awards.client import build_client as build_awards_client
from pipeline.ingest.awards.job import AwardsJob
from pipeline.ingest.context import IngestContext, ingest_run
from pipeline.ingest.opportunities import SOURCE as OPPORTUNITIES
from pipeline.ingest.opportunities.client import build_client as build_sam_client
from pipeline.ingest.opportunities.job import OpportunitiesJob
from pipeline.ingest.runs import JobAlreadyRunning
from pipeline.ingest.samples import sam_sample, usaspending_sample
from pipeline.settings import MissingSettingError, Settings, get_settings
from pipeline.verticals import vertical_naics

ingest_app = typer.Typer(
    help="Fetch notices (SAM.gov) and awards (USAspending). Every response is archived raw.",
    no_args_is_help=True,
)

DATE_FORMATS = ["%Y-%m-%d"]


def _log(message: str) -> None:
    typer.echo(f"[{datetime.now(UTC):%H:%M:%S}] {message}")


def _today() -> date:
    return datetime.now(UTC).date()


def _settings_or_exit(*, sam: bool = False) -> tuple[Settings, str, str]:
    """Settings, DATABASE_URL and SAM_API_KEY (empty unless `sam`). Missing → stop and say so."""
    settings = get_settings()
    try:
        database_url = settings.require_database_url()
        api_key = settings.require_sam_api_key() if sam else ""
    except MissingSettingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    return settings, database_url, api_key


def _run(
    settings: Settings,
    database_url: str,
    source: str,
    job: str,
    params: dict[str, object],
    action: Callable[[IngestContext], None],
) -> None:
    run_id = None
    try:
        with ingest_run(
            database_url, settings.raw_archive_path(), source, job, params, _log
        ) as ctx:
            run_id = ctx.run.id
            action(ctx)
    except JobAlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        typer.echo(f"{source} {job} failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _summary(database_url, run_id)


def _summary(database_url: str, run_id: int | None) -> None:
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


@ingest_app.command("opportunities")
def opportunities(
    since: Annotated[
        datetime | None,
        typer.Option(
            formats=DATE_FORMATS,
            metavar="YYYY-MM-DD",
            help="Backfill notices posted on or after this date.",
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        typer.Option(
            formats=DATE_FORMATS, metavar="YYYY-MM-DD", help="Backfill end date (default: today)."
        ),
    ] = None,
    backfill: Annotated[bool, typer.Option(help="Backfill the last 12 months.")] = False,
) -> None:
    """Notices from SAM.gov. With --since/--backfill: resumable backfill. Without: daily delta."""
    settings, database_url, api_key = _settings_or_exit(sam=True)
    today = _today()
    vertical = vertical_naics()
    if since is not None or backfill:
        start = since.date() if since else today - timedelta(days=365)
        end = until.date() if until else today

        def action(ctx: IngestContext) -> None:
            with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
                OpportunitiesJob(ctx, client, vertical).backfill(start, end)

        _run(settings, database_url, OPPORTUNITIES, "backfill", {}, action)
        return

    def delta(ctx: IngestContext) -> None:
        with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
            OpportunitiesJob(ctx, client, vertical).delta(today)

    _run(settings, database_url, OPPORTUNITIES, "delta", {}, delta)


@ingest_app.command("awards-backfill")
def awards_backfill(
    years: Annotated[int, typer.Option(min=1, max=20, help="Years of award history.")] = 5,
) -> None:
    """Contract awards and IDVs in the vertical from USAspending (no key needed)."""
    settings, database_url, _ = _settings_or_exit()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        client, close = build_awards_client(ctx)
        try:
            AwardsJob(ctx, client, vertical).backfill(years, _today())
        finally:
            close()

    _run(settings, database_url, AWARDS, "backfill", {"years": years}, action)


@ingest_app.command("awards-delta")
def awards_delta() -> None:
    """Awards changed since the last successful run (by USAspending last-modified date)."""
    settings, database_url, _ = _settings_or_exit()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        client, close = build_awards_client(ctx)
        try:
            AwardsJob(ctx, client, vertical).delta(_today())
        finally:
            close()

    _run(settings, database_url, AWARDS, "delta", {}, action)


@ingest_app.command("sample-fixtures")
def sample_fixtures() -> None:
    """Save small real responses as test fixtures and report how they differ from the docs.
    Uses 2 SAM.gov requests (skipped if SAM_API_KEY is missing) and 1 small USAspending download."""
    settings, database_url, _ = _settings_or_exit()
    today = _today()
    if settings.sam_api_key is None:
        typer.echo(
            "SAM_API_KEY is not set: skipping the SAM.gov sample. Add it to .env to include it."
        )
    else:
        api_key = settings.sam_api_key.get_secret_value()

        def sam(ctx: IngestContext) -> None:
            with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
                sam_sample(ctx, client, api_key, today)

        _run(settings, database_url, OPPORTUNITIES, "sample", {}, sam)

    def awards(ctx: IngestContext) -> None:
        client, close = build_awards_client(ctx)
        try:
            usaspending_sample(ctx, client, today)
        finally:
            close()

    _run(settings, database_url, AWARDS, "sample", {}, awards)

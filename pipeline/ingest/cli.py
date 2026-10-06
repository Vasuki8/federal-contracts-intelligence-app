"""`app ingest ...` commands."""

from datetime import datetime, timedelta
from typing import Annotated

import typer

from pipeline.ingest.awards import SOURCE as AWARDS
from pipeline.ingest.awards.client import build_client as build_awards_client
from pipeline.ingest.awards.job import AwardsJob
from pipeline.ingest.context import IngestContext
from pipeline.ingest.opportunities import SOURCE as OPPORTUNITIES
from pipeline.ingest.opportunities.client import build_client as build_sam_client
from pipeline.ingest.opportunities.extract import SOURCE as EXTRACT
from pipeline.ingest.opportunities.extract_job import ExtractJob
from pipeline.ingest.opportunities.extract_job import build_http as build_extract_http
from pipeline.ingest.opportunities.job import DEFAULT_BACKFILL_DAYS, OpportunitiesJob
from pipeline.ingest.samples import sam_sample, usaspending_sample
from pipeline.runner import run_job
from pipeline.runner import today as utc_today
from pipeline.settings import MissingSettingError, Settings, get_settings
from pipeline.verticals import vertical_naics

ingest_app = typer.Typer(
    help="Fetch notices (SAM.gov) and awards (USAspending). Every response is archived raw.",
    no_args_is_help=True,
)

DATE_FORMATS = ["%Y-%m-%d"]


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
    backfill: Annotated[
        bool, typer.Option(help="Resume the unfinished backfill, or start the last 12 months.")
    ] = False,
) -> None:
    """Notices from SAM.gov. Without options: daily delta.

    --backfill resumes the unfinished backfill (or starts 12 months; does nothing once
    complete). --since starts or resumes one from that date. --until fixes the end date."""
    settings, database_url, api_key = _settings_or_exit(sam=True)
    today = utc_today()
    vertical = vertical_naics()
    if since is not None or until is not None or backfill:
        start = since.date() if since else None
        end = until.date() if until else None

        def action(ctx: IngestContext) -> None:
            with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
                job = OpportunitiesJob(ctx, client, vertical)
                if end is not None:
                    job.backfill(start or end - timedelta(days=DEFAULT_BACKFILL_DAYS), end)
                else:
                    job.continue_backfill(today, start)

        run_job(settings, database_url, OPPORTUNITIES, "backfill", {}, action)
        return

    def delta(ctx: IngestContext) -> None:
        with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
            OpportunitiesJob(ctx, client, vertical).delta(today)

    run_job(settings, database_url, OPPORTUNITIES, "delta", {}, delta)


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
            AwardsJob(ctx, client, vertical).backfill(years, utc_today())
        finally:
            close()

    run_job(settings, database_url, AWARDS, "backfill", {"years": years}, action)


@ingest_app.command("awards-delta")
def awards_delta() -> None:
    """Awards changed since the last successful run (by USAspending last-modified date)."""
    settings, database_url, _ = _settings_or_exit()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        client, close = build_awards_client(ctx)
        try:
            AwardsJob(ctx, client, vertical).delta(utc_today())
        finally:
            close()

    run_job(settings, database_url, AWARDS, "delta", {}, action)


@ingest_app.command("sample-fixtures")
def sample_fixtures() -> None:
    """Save small real responses as test fixtures and report how they differ from the docs.
    Uses 2 SAM.gov requests (skipped if SAM_API_KEY is missing) and 1 small USAspending download."""
    settings, database_url, _ = _settings_or_exit()
    today = utc_today()
    if settings.sam_api_key is None:
        typer.echo(
            "SAM_API_KEY is not set: skipping the SAM.gov sample. Add it to .env to include it."
        )
    else:
        api_key = settings.sam_api_key.get_secret_value()

        def sam(ctx: IngestContext) -> None:
            with build_sam_client(ctx, api_key, settings.sam_daily_request_limit) as client:
                sam_sample(ctx, client, api_key, today)

        run_job(settings, database_url, OPPORTUNITIES, "sample", {}, sam)

    def awards(ctx: IngestContext) -> None:
        client, close = build_awards_client(ctx)
        try:
            usaspending_sample(ctx, client, today)
        finally:
            close()

    run_job(settings, database_url, AWARDS, "sample", {}, awards)


@ingest_app.command("notice-extract")
def notice_extract(
    force: Annotated[bool, typer.Option(help="Load even if this exact file was loaded.")] = False,
) -> None:
    """Every active notice from SAM.gov's daily CSV, with descriptions (no key needed)."""
    settings, database_url, _ = _settings_or_exit()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        with build_extract_http() as http:
            ExtractJob(ctx, http, vertical).daily(force=force)

    run_job(settings, database_url, EXTRACT, "daily", {}, action)


@ingest_app.command("notice-archive")
def notice_archive(
    fiscal_year: Annotated[
        int, typer.Option(min=2000, max=2100, help="Fiscal year of the archive file.")
    ],
    since: Annotated[
        datetime | None,
        typer.Option(
            formats=DATE_FORMATS,
            metavar="YYYY-MM-DD",
            help="Only notices posted on or after this date.",
        ),
    ] = None,
    force: Annotated[bool, typer.Option(help="Load even if this exact file was loaded.")] = False,
) -> None:
    """Archived notices for one fiscal year from SAM.gov's archive CSV (~1 GB download)."""
    settings, database_url, _ = _settings_or_exit()
    vertical = vertical_naics()
    start = since.date() if since else None

    def action(ctx: IngestContext) -> None:
        with build_extract_http() as http:
            ExtractJob(ctx, http, vertical).archive(fiscal_year, start, force=force)

    run_job(settings, database_url, EXTRACT, "archive", {"fiscal_year": fiscal_year}, action)

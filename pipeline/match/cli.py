"""`app match ...` commands."""

import typer

from pipeline.ingest.context import IngestContext
from pipeline.match import SOURCE
from pipeline.match.config import load_matching_config
from pipeline.match.job import MatchJob
from pipeline.runner import run_job
from pipeline.runner import today as utc_today
from pipeline.settings import MissingSettingError, Settings, get_settings
from pipeline.verticals import vertical_naics

match_app = typer.Typer(
    help="Link notices to the contracts they replace (incumbents) and check accuracy.",
    no_args_is_help=True,
)


def _settings_or_exit() -> tuple[Settings, str]:
    settings = get_settings()
    try:
        return settings, settings.require_database_url()
    except MissingSettingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@match_app.command("notices")
def notices() -> None:
    """Score incumbent candidates for every active vertical notice and save what to show."""
    settings, database_url = _settings_or_exit()
    cfg = load_matching_config()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        MatchJob(ctx, cfg, vertical, utc_today()).run()

    params: dict[str, object] = {"matcher_version": cfg.matcher_version}
    run_job(settings, database_url, SOURCE, "notices", params, action)

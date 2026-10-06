"""`app match ...` and `app recompetes ...` commands."""

import time
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from pipeline.db import connect
from pipeline.ingest.context import IngestContext
from pipeline.match import SOURCE
from pipeline.match.config import load_matching_config
from pipeline.match.diagnose import diagnose
from pipeline.match.eval import evaluate, render
from pipeline.match.job import MatchJob
from pipeline.match.labels import (
    LABELS_FILE,
    LabelError,
    label_sheet,
    load_labels,
    merge_labels,
    read_answers,
    save_labels,
    write_sheet,
)
from pipeline.match.reset import reset_automatic_matches
from pipeline.recompetes import SOURCE as RECOMPETES
from pipeline.recompetes import refresh_recompetes
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


@match_app.command("diagnose")
def diagnose_command() -> None:
    """Check the matcher's inputs: office-code overlap, notice coverage, text, results."""
    _, database_url = _settings_or_exit()
    with connect(database_url) as conn:
        for line in diagnose(conn, vertical_naics(), load_matching_config()):
            typer.echo(line)


recompetes_app = typer.Typer(
    help="Contracts in the vertical ending in 6-24 months (the recompete calendar).",
    no_args_is_help=True,
)


@recompetes_app.command("refresh")
def refresh() -> None:
    """Rebuild the recompetes table."""
    settings, database_url = _settings_or_exit()
    cfg = load_matching_config()
    vertical = vertical_naics()

    def action(ctx: IngestContext) -> None:
        started = time.monotonic()
        count = refresh_recompetes(ctx.data, vertical, cfg, utc_today())
        ctx.data.commit()
        ctx.run.counters.rows_upserted += count
        ctx.log(f"{count:,} recompetes in {time.monotonic() - started:.1f} s.")

    run_job(settings, database_url, RECOMPETES, "refresh", {}, action)


@match_app.command("label-sheet")
def label_sheet_command(
    out: Annotated[Path, typer.Option(help="Where to write the CSV.")] = Path(
        "matching-labels.csv"
    ),
    size: Annotated[int, typer.Option(min=1, max=1000, help="Notices to include.")] = 100,
    seed: Annotated[str, typer.Option(help="Changes which notices are sampled.")] = "m2",
) -> None:
    """Write the labeling spreadsheet: notices with up to 5 candidate contracts each."""
    _, database_url = _settings_or_exit()
    with connect(database_url) as conn:
        written = write_sheet(label_sheet(conn, size, seed), out)
    typer.echo(f"Wrote {written} notices to {out}.")
    typer.echo(
        "In the `answer` column put the number of the right candidate (1-5), `other` if the "
        "incumbent isn't listed, `none` if it's new work, or `unsure`."
    )


@match_app.command("import-labels")
def import_labels(
    sheet: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Filled-in sheet.")],
    labels: Annotated[Path, typer.Option(help="The labels file to update.")] = LABELS_FILE,
) -> None:
    """Check a filled-in labeling sheet and merge its answers into the labels file."""
    try:
        new = read_answers(sheet, "spreadsheet", utc_today())
    except LabelError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    merged = merge_labels(load_labels(labels), new)
    save_labels(merged, labels)
    counts = Counter(label.answer for label in new)
    typer.echo(
        f"Imported {len(new)} answers ({dict(sorted(counts.items()))}); "
        f"{len(merged)} labeled notices in {labels}."
    )


@match_app.command("eval")
def eval_command(
    labels: Annotated[Path, typer.Option(help="The labels file.")] = LABELS_FILE,
    report: Annotated[
        Path | None, typer.Option(help="Also write the report here (Markdown).")
    ] = None,
) -> None:
    """Precision and recall of the incumbents the app shows, against the labels."""
    _, database_url = _settings_or_exit()
    cfg = load_matching_config()
    with connect(database_url) as conn:
        text = render(evaluate(conn, load_labels(labels), cfg, utc_today()))
    typer.echo(text)
    if report is not None:
        report.write_text(text, encoding="utf-8")


@match_app.command("reset")
def reset_command() -> None:
    """Remove all automatic matches and free their space (human decisions are kept)."""
    _, database_url = _settings_or_exit()
    with connect(database_url, autocommit=True) as conn:
        typer.echo(reset_automatic_matches(conn))

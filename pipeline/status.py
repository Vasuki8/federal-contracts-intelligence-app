"""`app status`: last run, errors, backfill progress, request budget and row counts."""

from datetime import UTC, datetime

import psycopg
from psycopg.rows import TupleRow

from pipeline.ingest.awards import SOURCE as AWARDS
from pipeline.ingest.budget import requests_today
from pipeline.ingest.opportunities import SOURCE as OPPORTUNITIES

SOURCES = (OPPORTUNITIES, AWARDS)
TABLES = (
    ("notices", "notices"),
    ("notices (active)", "notices WHERE active"),
    ("notice_versions", "notice_versions"),
    ("awards", "awards"),
    ("entities", "entities"),
    ("agencies", "agencies"),
    ("offices", "offices"),
    ("naics (in vertical)", "naics WHERE in_vertical"),
    ("raw_files", "raw_files"),
)


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0][:160] if text and text.strip() else ""


def _when(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def build_report(
    conn: psycopg.Connection[TupleRow], sam_daily_limit: int, now: datetime
) -> list[str]:
    lines: list[str] = []
    runs = conn.execute(
        """
        SELECT DISTINCT ON (source, job) source, job, status, started_at, finished_at,
               rows_in, rows_upserted, requests_made, error
        FROM ingest_runs ORDER BY source, job, started_at DESC
        """
    ).fetchall()
    lines.append("Last run per job")
    if not runs:
        lines.append("  No ingest runs yet. Start with: uv run app ingest awards-backfill")
    for source, job, status, started, finished, rows_in, written, requests, error in runs:
        lines.append(
            f"  {source} {job}: {status}, started {_when(started)}, finished {_when(finished)}; "
            f"{rows_in} rows read, {written} written, {requests} requests"
        )
        if error:
            lines.append(f"    {'note' if status == 'partial' else 'error'}: {_first_line(error)}")

    lines.append("Last error per source")
    for source in SOURCES:
        row = conn.execute(
            """
            SELECT job, started_at, error FROM ingest_runs
            WHERE source = %s AND status = 'failed' ORDER BY started_at DESC LIMIT 1
            """,
            (source,),
        ).fetchone()
        detail = f"{row[0]} at {_when(row[1])}: {_first_line(row[2])}" if row else "none"
        lines.append(f"  {source}: {detail}")

    lines.append("Backfill chunks")
    chunks = conn.execute(
        """
        SELECT source, count(*) FILTER (WHERE status = 'done'), count(*)
        FROM ingest_chunks GROUP BY source ORDER BY source
        """
    ).fetchall()
    if not chunks:
        lines.append("  none yet")
    for source, done, total in chunks:
        lines.append(f"  {source}: {done} of {total} done")

    used = requests_today(conn, OPPORTUNITIES, now)
    lines.append(f"SAM.gov requests today (UTC): {used} of {sam_daily_limit}")

    lines.append("Rows")
    for label, from_clause in TABLES:
        row = conn.execute(f"SELECT count(*) FROM {from_clause}").fetchone()
        lines.append(f"  {label:<22}{row[0] if row else 0:>12,}")
    size = conn.execute("SELECT coalesce(sum(bytes), 0) FROM raw_files").fetchone()
    lines.append(f"  {'raw archive (MB)':<22}{(size[0] if size else 0) / 1_000_000:>12,.1f}")
    return lines

"""Awards backfill (by action date, one chunk per fiscal year) and daily delta (by last
modified date). A chunk whose file hits USAspending's 500,000-row cap is split by NAICS
code, then by quarter."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from pipeline.ingest.awards import SOURCE
from pipeline.ingest.awards.client import (
    AwardsClient,
    DateType,
    DownloadFailed,
    download_filters,
)
from pipeline.ingest.awards.parser import read_award_zip
from pipeline.ingest.awards.store import load_awards, seed_vertical_naics
from pipeline.ingest.context import IngestContext, update_run_params
from pipeline.ingest.http import ApiError
from pipeline.ingest.runs import get_chunk, save_chunk

OVERLAP_DAYS = 3
QUARTER_DAYS = 92


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 February
        return today.replace(year=today.year - years, day=28)


def fiscal_year_windows(since: date, until: date) -> list[Window]:
    """[since, until] cut at federal fiscal-year boundaries (FY starts 1 October)."""
    windows = []
    start = since
    while start <= until:
        fy_end = date(start.year if start.month < 10 else start.year + 1, 9, 30)
        end = min(fy_end, until)
        windows.append(Window(start, end))
        start = end + timedelta(days=1)
    return windows


def quarters(window: Window) -> list[Window]:
    parts = []
    start = window.start
    while start <= window.end:
        end = min(window.end, start + timedelta(days=QUARTER_DAYS - 1))
        parts.append(Window(start, end))
        start = end + timedelta(days=1)
    return parts


def latest_through(ctx: IngestContext) -> date | None:
    row = ctx.meta.execute(
        """
        SELECT max((params ->> 'through')::date) FROM ingest_runs
        WHERE source = %s AND status = 'succeeded' AND params ? 'through'
        """,
        (SOURCE,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


@dataclass
class AwardsJob:
    ctx: IngestContext
    client: AwardsClient
    vertical: frozenset[str]
    _reported_missing: set[str] = field(default_factory=set)

    def backfill(self, years: int, today: date) -> None:
        since = years_ago(today, years)
        update_run_params(self.ctx, {"since": since.isoformat(), "through": today.isoformat()})
        seed_vertical_naics(self.ctx.data, self.vertical)
        self.ctx.data.commit()
        for window in fiscal_year_windows(since, today):
            self._load(window, sorted(self.vertical), "action_date", checkpoint=True)

    def delta(self, today: date) -> None:
        start = (latest_through(self.ctx) or today) - timedelta(days=OVERLAP_DAYS)
        update_run_params(self.ctx, {"since": start.isoformat(), "through": today.isoformat()})
        seed_vertical_naics(self.ctx.data, self.vertical)
        self.ctx.data.commit()
        self._load(
            Window(start, today), sorted(self.vertical), "last_modified_date", checkpoint=False
        )

    def _load(
        self, window: Window, codes: list[str], date_type: DateType, checkpoint: bool
    ) -> None:
        key = f"{date_type}:{window.start}:{window.end}:naics:{'+'.join(codes)}"
        chunk = get_chunk(self.ctx.data, SOURCE, key) if checkpoint else None
        if chunk is not None and chunk.status == "done":
            if chunk.state.get("split"):
                self._load_parts(window, codes, date_type, checkpoint)
            return
        filters = download_filters(window.start, window.end, codes, date_type)
        pending = chunk.state.get("file_name") if chunk is not None else None
        status, file_name = self._finished_status(filters, pending, key if checkpoint else None)
        download = self.client.fetch(status, {"filters": filters, "file_name": file_name})

        if download.truncated:
            if len(codes) == 1 and window.days <= QUARTER_DAYS:
                raise DownloadFailed(f"{key} still exceeds USAspending's 500,000-row limit")
            self.ctx.log(f"{key} hit the 500,000-row cap; splitting it.")
            if checkpoint:
                self._checkpoint(key, "done", {"split": True, "file_name": file_name}, 0, 0)
            self._load_parts(window, codes, date_type, checkpoint)
            return

        rows_in = written = 0
        work_dir = self.ctx.archive_root / ".tmp" / "extract"
        for frame in read_award_zip(self.ctx.archive_root / Path(download.file.path), work_dir):
            self._report_missing(frame.missing_columns)
            self.ctx.keepalive()
            result = load_awards(self.ctx.data, frame.frame, download.file.id, self.vertical)
            rows_in += result.rows_in
            written += result.awards_upserted
        if checkpoint:
            state = {"file_name": file_name, "raw_file_id": download.file.id}
            self._checkpoint(key, "done", state, rows_in, written)
        self.ctx.data.commit()
        self.ctx.run.counters.rows_in += rows_in
        self.ctx.run.counters.rows_upserted += written
        self.ctx.log(f"{key}: {rows_in} rows, {written} awards written")

    def _finished_status(
        self, filters: dict[str, object], pending: object, key: str | None
    ) -> tuple[dict[str, object], str]:
        """Resume a download a crashed run already requested; otherwise request a new one."""
        if isinstance(pending, str):
            try:
                return self.client.wait(pending), pending
            except (DownloadFailed, ApiError) as exc:
                self.ctx.log(f"Earlier download {pending} is not usable ({exc}); requesting again.")
        file_name = self.client.start(filters)
        if key is not None:
            self._checkpoint(key, "in_progress", {"file_name": file_name}, 0, 0)
            self.ctx.data.commit()
        return self.client.wait(file_name), file_name

    def _load_parts(
        self, window: Window, codes: list[str], date_type: DateType, checkpoint: bool
    ) -> None:
        if len(codes) > 1:
            for code in codes:
                self._load(window, [code], date_type, checkpoint)
        else:
            for part in quarters(window):
                self._load(part, codes, date_type, checkpoint)

    def _checkpoint(
        self, key: str, status: str, state: dict[str, object], rows_in: int, written: int
    ) -> None:
        save_chunk(
            self.ctx.data,
            source=SOURCE,
            key=key,
            status="done" if status == "done" else "in_progress",
            state=state,
            run_id=self.ctx.run.id,
            rows_in=rows_in,
            rows_upserted=written,
        )

    def _report_missing(self, columns: tuple[str, ...]) -> None:
        new = set(columns) - self._reported_missing
        if new:
            self._reported_missing |= new
            self.ctx.log(
                f"USAspending file lacks expected columns (loaded as empty): {sorted(new)}"
            )

"""Backfill and daily delta for SAM.gov notices.

Backfill: one resumable chunk per (posted-date window ≤ 1 year, vertical NAICS code).
Delta: notices posted since the last successful run (minus an overlap), all NAICS in
one query (cheaper than 11 per-code queries under a 10/day limit), filtered locally.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from pipeline.ingest.context import IngestContext, update_run_params
from pipeline.ingest.opportunities import SOURCE
from pipeline.ingest.opportunities.client import PAGE_LIMIT, OpportunitiesClient
from pipeline.ingest.opportunities.parser import parse_page
from pipeline.ingest.opportunities.store import upsert_notices
from pipeline.ingest.runs import get_chunk, save_chunk

OVERLAP_DAYS = 2
MAX_WINDOW_DAYS = 365  # docs: postedFrom..postedTo at most 1 year apart


@dataclass(frozen=True)
class Window:
    start: date
    end: date


def windows(since: date, until: date) -> list[Window]:
    """Split [since, until] into consecutive windows of at most MAX_WINDOW_DAYS days."""
    if since > until:
        raise ValueError(f"--since ({since}) is after the end date ({until})")
    result = []
    start = since
    while start <= until:
        end = min(until, start + timedelta(days=MAX_WINDOW_DAYS - 1))
        result.append(Window(start, end))
        start = end + timedelta(days=1)
    return result


def latest_posted_to(ctx: IngestContext) -> date | None:
    row = ctx.meta.execute(
        """
        SELECT max((params ->> 'posted_to')::date) FROM ingest_runs
        WHERE source = %s AND status = 'succeeded' AND params ? 'posted_to'
        """,
        (SOURCE,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


@dataclass
class OpportunitiesJob:
    ctx: IngestContext
    client: OpportunitiesClient
    vertical: frozenset[str]
    _reported_fields: set[str] = field(default_factory=set)

    def backfill(self, since: date, until: date) -> None:
        update_run_params(
            self.ctx, {"posted_from": since.isoformat(), "posted_to": until.isoformat()}
        )
        for window in windows(since, until):
            for code in sorted(self.vertical):
                key = f"posted:{window.start}:{window.end}:naics:{code}"
                chunk = get_chunk(self.ctx.data, SOURCE, key)
                if chunk is not None and chunk.status == "done":
                    continue
                offset = int(chunk.state.get("next_offset", 0)) if chunk else 0
                self._fetch(window, code, offset, key)

    def delta(self, today: date) -> None:
        watermark = latest_posted_to(self.ctx)
        start = (watermark or today) - timedelta(days=OVERLAP_DAYS)
        start = max(start, today - timedelta(days=MAX_WINDOW_DAYS - 1))
        update_run_params(
            self.ctx, {"posted_from": start.isoformat(), "posted_to": today.isoformat()}
        )
        self._fetch(Window(start, today), None, 0, None)

    def _fetch(self, window: Window, ncode: str | None, offset: int, chunk_key: str | None) -> None:
        """Page through one query. Each page commits with its checkpoint, so a crash or an
        exhausted budget resumes at the next page."""
        seen: set[str] = set()
        total = 0
        started_at_zero = offset == 0
        while True:
            payload = self.client.search(
                posted_from=window.start, posted_to=window.end, offset=offset, ncode=ncode
            )
            page = parse_page(payload)
            total = page.total_records
            self._report_unknown(page.unknown_fields)
            wanted = [n for n in page.notices if n.in_naics(self.vertical)]
            result = upsert_notices(self.ctx.data, wanted, self.client.last_raw_file_id)
            seen.update(n.notice_id for n in page.notices)
            # Docs: `offset` "indicates the page index" (verify live; see data-sources.md).
            done = not page.notices or (offset + 1) * PAGE_LIMIT >= total
            if chunk_key is not None:
                save_chunk(
                    self.ctx.data,
                    source=SOURCE,
                    key=chunk_key,
                    status="done" if done else "in_progress",
                    state={"next_offset": offset + 1, "total_records": total},
                    run_id=self.ctx.run.id,
                    rows_in=len(page.notices),
                    rows_upserted=result.written,
                )
            self.ctx.data.commit()
            self.ctx.run.counters.rows_in += len(page.notices)
            self.ctx.run.counters.rows_upserted += result.written
            if done:
                break
            offset += 1
        if started_at_zero and len(seen) < total:
            self.ctx.log(
                f"Warning: got {len(seen)} distinct notices but totalRecords was {total} "
                f"({ncode or 'all NAICS'}, {window.start}..{window.end}). Paging may be off."
            )

    def _report_unknown(self, fields: set[str]) -> None:
        new = fields - self._reported_fields
        if new:
            self._reported_fields |= new
            self.ctx.log(f"SAM returned fields the parser doesn't know yet: {sorted(new)}")

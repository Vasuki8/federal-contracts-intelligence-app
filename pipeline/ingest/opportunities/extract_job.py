"""Load SAM.gov's CSV extracts: `app ingest notice-extract` (daily, every active notice)
and `app ingest notice-archive --fiscal-year N` (notices archived that year).

Per file: stream it to disk, skip it if a successful run already loaded the same bytes,
keep the vertical rows, archive exactly those rows (gzipped, with the full file's
sha256, size and HTTP validators), then merge them into `notices` in batches."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from pipeline.ingest.archive import archive_bytes
from pipeline.ingest.context import IngestContext, update_run_params
from pipeline.ingest.http import ApiError
from pipeline.ingest.opportunities.extract import (
    AUTHORITATIVE,
    DAILY_URL,
    ENCODING,
    SOURCE,
    archive_url,
    as_record,
    gzip_bytes,
    in_scope,
    parse_row,
    read_rows,
    sha256_file,
    vertical_csv,
)
from pipeline.ingest.opportunities.store import IncomingNotice, UpsertResult, upsert_notices

BATCH_SIZE = 500
ATTEMPTS = 3


@dataclass(frozen=True)
class FileInfo:
    url: str
    http_status: int
    etag: str | None
    last_modified: str | None
    size: int
    sha256: str


@dataclass
class ExtractJob:
    ctx: IngestContext
    http: httpx.Client
    vertical: frozenset[str]
    sleep: Callable[[float], None] = time.sleep
    totals: UpsertResult = field(default_factory=UpsertResult)

    def daily(self, force: bool = False) -> None:
        self._load(DAILY_URL, "Daily extract", since=None, force=force)

    def archive(self, fiscal_year: int, since: date | None = None, force: bool = False) -> None:
        self._load(archive_url(fiscal_year), f"FY{fiscal_year} archive", since, force)

    def _load(self, url: str, label: str, since: date | None, force: bool) -> None:
        update_run_params(self.ctx, {"url": url, "since": since.isoformat() if since else None})
        tmp_dir = self.ctx.archive_root / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"sam-extract-{uuid.uuid4().hex}.csv"
        try:
            info = self._download(url, tmp)
            if not force and self._already_loaded(info):
                self.ctx.log(f"{label}: this file (sha256 {info.sha256[:12]}…) is already loaded.")
                return
            header, rows = read_rows(tmp)
            if header.unknown:
                self.ctx.log(f"{label}: new columns not loaded yet: {list(header.unknown)}")
            total, kept = 0, []
            for row in rows:
                total += 1
                if in_scope(as_record(header, row), self.vertical, since):
                    kept.append(row)
                if total % 20_000 == 0:
                    self.ctx.keepalive()
        finally:
            tmp.unlink(missing_ok=True)
        archived = archive_bytes(
            self.ctx.meta,
            root=self.ctx.archive_root,
            source=SOURCE,
            content=gzip_bytes(vertical_csv(header, kept)),
            suffix=".vertical.csv.gz",
            run_id=self.ctx.run.id,
            http_status=info.http_status,
            request_params=self._provenance(info, total, len(kept), since),
        )
        result = self._upsert([as_record(header, row) for row in kept], archived.id)
        self.ctx.log(
            f"{label}: {total:,} rows in the file, {len(kept):,} in the vertical: "
            f"{result.inserted:,} new, {result.changed:,} amended, {result.updated:,} updated, "
            f"{result.unchanged:,} unchanged."
        )

    def _upsert(self, records: list[dict[str, str]], raw_file_id: int) -> UpsertResult:
        result = UpsertResult()
        for start in range(0, len(records), BATCH_SIZE):
            batch: list[IncomingNotice] = [
                item
                for record in records[start : start + BATCH_SIZE]
                if (item := parse_row(record)) is not None
            ]
            self.ctx.keepalive()
            part = upsert_notices(self.ctx.data, batch, raw_file_id, AUTHORITATIVE)
            self.ctx.data.commit()
            for name in ("inserted", "changed", "updated", "unchanged"):
                setattr(result, name, getattr(result, name) + getattr(part, name))
            self.ctx.run.counters.rows_in += len(batch)
            self.ctx.run.counters.rows_upserted += part.written
        return result

    def _download(self, url: str, target: Path) -> FileInfo:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with self.http.stream("GET", url) as response:
                    if response.status_code >= 500 and attempt < ATTEMPTS:
                        raise httpx.TransportError(f"HTTP {response.status_code}")
                    if response.is_error:
                        raise ApiError(f"GET {url} returned HTTP {response.status_code}")
                    size = 0
                    with target.open("wb") as out:
                        for block in response.iter_bytes():
                            out.write(block)
                            size += len(block)
                            self.ctx.keepalive()
                    self.ctx.run.counters.requests_made += 1
                    return FileInfo(
                        url=url,
                        http_status=response.status_code,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        size=size,
                        sha256=sha256_file(target),
                    )
            except httpx.TransportError as exc:
                self.ctx.run.counters.requests_made += 1
                if attempt == ATTEMPTS:
                    raise ApiError(f"GET {url} failed: {exc!r}") from exc
                self.sleep(5.0 * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _already_loaded(self, info: FileInfo) -> bool:
        row = self.ctx.meta.execute(
            """
            SELECT 1 FROM raw_files f JOIN ingest_runs r ON r.id = f.run_id
            WHERE f.source = %s AND r.status = 'succeeded'
              AND f.request_params ->> 'url' = %s
              AND f.request_params ->> 'file_sha256' = %s
            LIMIT 1
            """,
            (SOURCE, info.url, info.sha256),
        ).fetchone()
        return row is not None

    @staticmethod
    def _provenance(info: FileInfo, total: int, kept: int, since: date | None) -> dict[str, Any]:
        return {
            "url": info.url,
            "etag": info.etag,
            "last_modified": info.last_modified,
            "file_bytes": info.size,
            "file_sha256": info.sha256,
            "file_encoding": ENCODING,
            "rows_in_file": total,
            "rows_kept": kept,
            "kept": "vertical NAICS rows, unchanged" + (f", posted since {since}" if since else ""),
        }


def build_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True)

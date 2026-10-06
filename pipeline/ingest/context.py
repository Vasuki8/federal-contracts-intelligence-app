"""Shared plumbing for ingest jobs: connections, the current run, response archiving."""

import contextlib
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.db import connect
from pipeline.ingest.archive import ArchivedFile, archive_bytes
from pipeline.ingest.budget import BudgetExhausted
from pipeline.ingest.runs import Run, RunStatus, fail_stale_runs, finish_run, job_lock, start_run

Log = Callable[[str], None]

# Hosted Postgres (Neon) drops connections that sit idle while we wait on a slow upstream
# (a USAspending file can take 15+ minutes to build), so long waits ping both connections.
KEEPALIVE_SECONDS = 60.0


@dataclass
class IngestContext:
    """`meta` autocommits (runs, raw_files, budget) so bookkeeping survives a failed chunk;
    `data` is transactional so a chunk's rows and its checkpoint commit together."""

    meta: psycopg.Connection[TupleRow]
    data: psycopg.Connection[TupleRow]
    archive_root: Path
    run: Run
    log: Log
    clock: Callable[[], float] = time.monotonic
    _last_ping: float = field(default_factory=time.monotonic)

    def keepalive(self) -> None:
        """Ping both connections if they've been idle for KEEPALIVE_SECONDS. Never ends
        an open transaction that holds work; only the empty one the ping itself opens."""
        now = self.clock()
        if now - self._last_ping < KEEPALIVE_SECONDS:
            return
        self.meta.execute("SELECT 1")
        was_idle = self.data.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        self.data.execute("SELECT 1")
        if was_idle:
            self.data.rollback()
        self._last_ping = now


class ResponseArchiver:
    """HttpClient recorder: archives every attempt (errors included) and counts requests."""

    def __init__(self, ctx: IngestContext, source: str, suffix: str, compress: bool) -> None:
        self._ctx = ctx
        self._source = source
        self._suffix = suffix
        self._compress = compress
        self.last: ArchivedFile | None = None

    def __call__(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, Any],
        response: httpx.Response | None,
        error: Exception | None,
    ) -> None:
        self._ctx.run.counters.requests_made += 1
        request: dict[str, Any] = {"method": method, "url": url, "params": dict(params)}
        if response is not None and (limits := rate_limit_headers(response)):
            request["rate_limit"] = limits
        if response is not None:
            content = response.content
            status: int | None = response.status_code
        else:
            content = repr(error).encode()
            status = None
        self.last = archive_bytes(
            self._ctx.meta,
            root=self._ctx.archive_root,
            source=self._source,
            content=content,
            suffix=self._suffix if status is not None and status < 400 else ".error.txt",
            run_id=self._ctx.run.id,
            http_status=status,
            request_params=request,
            compress=self._compress,
        )


def rate_limit_headers(response: httpx.Response) -> dict[str, str]:
    """api.sam.gov reports the key's limit in X-RateLimit-* headers (when it sends them)."""
    return {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower().startswith("x-ratelimit-")
    }


@contextmanager
def ingest_run(
    database_url: str,
    archive_root: Path,
    source: str,
    job: str,
    params: Mapping[str, Any],
    log: Log,
) -> Iterator[IngestContext]:
    """Lock the job, open a run row, and close it as succeeded / partial / failed.

    BudgetExhausted ends the run as `partial` (not an error); anything else is `failed`.
    """
    with (
        connect(database_url, autocommit=True) as meta,
        connect(database_url) as data,
        job_lock(meta, source, job),
    ):
        stale = fail_stale_runs(meta, source, job)
        if stale:
            log(f"Marked {stale} earlier '{source} {job}' run(s) that never finished as failed.")
        run = start_run(meta, source, job, params)
        ctx = IngestContext(meta=meta, data=data, archive_root=archive_root, run=run, log=log)
        try:
            yield ctx
        except BudgetExhausted as exc:
            _rollback_quietly(data)
            log(str(exc))
            _finish(database_url, meta, run, "partial", str(exc))
        except BaseException as exc:
            _rollback_quietly(data)
            _finish(database_url, meta, run, "failed", f"{type(exc).__name__}: {exc}")
            raise
        else:
            _finish(database_url, meta, run, "succeeded", None)


def _rollback_quietly(conn: psycopg.Connection[TupleRow]) -> None:
    """Roll back if the connection is still alive; a dead one has nothing to roll back."""
    with contextlib.suppress(psycopg.Error):
        conn.rollback()


def _finish(
    database_url: str,
    meta: psycopg.Connection[TupleRow],
    run: Run,
    status: RunStatus,
    error: str | None,
) -> None:
    """Record the run's result, on a fresh connection if `meta` has been dropped."""
    try:
        finish_run(meta, run, status, error=error)
    except psycopg.Error:
        with connect(database_url, autocommit=True) as fresh:
            finish_run(fresh, run, status, error=error)


def update_run_params(ctx: IngestContext, params: Mapping[str, Any]) -> None:
    """Merge values into the run's params (e.g. the window a delta actually covered)."""
    ctx.meta.execute(
        "UPDATE ingest_runs SET params = params || %s WHERE id = %s",
        (Jsonb(dict(params)), ctx.run.id),
    )

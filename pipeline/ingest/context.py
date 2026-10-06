"""Shared plumbing for ingest jobs: connections, the current run, response archiving."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.db import connect
from pipeline.ingest.archive import ArchivedFile, archive_bytes
from pipeline.ingest.budget import BudgetExhausted
from pipeline.ingest.runs import Run, finish_run, job_lock, start_run

Log = Callable[[str], None]


@dataclass
class IngestContext:
    """`meta` autocommits (runs, raw_files, budget) so bookkeeping survives a failed chunk;
    `data` is transactional so a chunk's rows and its checkpoint commit together."""

    meta: psycopg.Connection[TupleRow]
    data: psycopg.Connection[TupleRow]
    archive_root: Path
    run: Run
    log: Log


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
        request = {"method": method, "url": url, "params": dict(params)}
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
        run = start_run(meta, source, job, params)
        ctx = IngestContext(meta=meta, data=data, archive_root=archive_root, run=run, log=log)
        try:
            yield ctx
        except BudgetExhausted as exc:
            data.rollback()
            log(str(exc))
            finish_run(meta, run, "partial", error=str(exc))
        except BaseException as exc:
            data.rollback()
            finish_run(meta, run, "failed", error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            finish_run(meta, run, "succeeded")


def update_run_params(ctx: IngestContext, params: Mapping[str, Any]) -> None:
    """Merge values into the run's params (e.g. the window a delta actually covered)."""
    ctx.meta.execute(
        "UPDATE ingest_runs SET params = params || %s WHERE id = %s",
        (Jsonb(dict(params)), ctx.run.id),
    )

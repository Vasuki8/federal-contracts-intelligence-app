"""Run bookkeeping: `ingest_runs` rows, per-job locks and resumable chunk checkpoints."""

import contextlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

RunStatus = Literal["succeeded", "partial", "failed"]


class JobAlreadyRunning(RuntimeError):
    def __init__(self, source: str, job: str) -> None:
        super().__init__(
            f"Another '{source} {job}' run holds the lock. Try again when it finishes."
        )


@dataclass
class RunCounters:
    rows_in: int = 0
    rows_upserted: int = 0
    requests_made: int = 0


@dataclass
class Run:
    id: int
    source: str
    job: str
    counters: RunCounters = field(default_factory=RunCounters)


@contextmanager
def job_lock(conn: psycopg.Connection[TupleRow], source: str, job: str) -> Iterator[None]:
    """Session-level advisory lock so overlapping cron runs of one job can't collide."""
    key = f"ingest:{source}:{job}"
    row = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)).fetchone()
    if not row or not row[0]:
        raise JobAlreadyRunning(source, job)
    try:
        yield
    finally:
        # If the session is gone, Postgres has already released its locks.
        if not conn.closed:
            with contextlib.suppress(psycopg.OperationalError):
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


STALE_RUN_ERROR = "Interrupted: the run ended without recording a result."


def fail_stale_runs(conn: psycopg.Connection[TupleRow], source: str, job: str) -> int:
    """Mark earlier runs of this job still 'running' as failed. Call only while holding the
    job's lock: then no other run of it can be alive."""
    cursor = conn.execute(
        """
        UPDATE ingest_runs SET status = 'failed', finished_at = now(),
            error = coalesce(error, %s)
        WHERE source = %s AND job = %s AND status = 'running'
        """,
        (STALE_RUN_ERROR, source, job),
    )
    return cursor.rowcount


def start_run(
    conn: psycopg.Connection[TupleRow], source: str, job: str, params: Mapping[str, Any]
) -> Run:
    row = conn.execute(
        "INSERT INTO ingest_runs (source, job, params) VALUES (%s, %s, %s) RETURNING id",
        (source, job, Jsonb(dict(params))),
    ).fetchone()
    assert row is not None
    return Run(id=int(row[0]), source=source, job=job)


def finish_run(
    conn: psycopg.Connection[TupleRow], run: Run, status: RunStatus, error: str | None = None
) -> None:
    conn.execute(
        """
        UPDATE ingest_runs
        SET status = %s, finished_at = now(), rows_in = %s, rows_upserted = %s,
            requests_made = %s, error = %s
        WHERE id = %s
        """,
        (
            status,
            run.counters.rows_in,
            run.counters.rows_upserted,
            run.counters.requests_made,
            error,
            run.id,
        ),
    )


def last_success_param(
    conn: psycopg.Connection[TupleRow], source: str, job: str, param: str
) -> str | None:
    """A params value from the latest succeeded run of a job (e.g. its window end)."""
    row = conn.execute(
        """
        SELECT params ->> %s FROM ingest_runs
        WHERE source = %s AND job = %s AND status = 'succeeded'
        ORDER BY started_at DESC LIMIT 1
        """,
        (param, source, job),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


@dataclass(frozen=True)
class Chunk:
    key: str
    status: Literal["in_progress", "done"]
    state: dict[str, Any]


def get_chunk(conn: psycopg.Connection[TupleRow], source: str, key: str) -> Chunk | None:
    row = conn.execute(
        "SELECT status, state FROM ingest_chunks WHERE source = %s AND chunk_key = %s",
        (source, key),
    ).fetchone()
    if row is None:
        return None
    return Chunk(key=key, status=row[0], state=dict(row[1]))


def save_chunk(
    conn: psycopg.Connection[TupleRow],
    *,
    source: str,
    key: str,
    status: Literal["in_progress", "done"],
    state: Mapping[str, Any],
    run_id: int,
    rows_in: int = 0,
    rows_upserted: int = 0,
) -> None:
    """Record chunk progress. Commit it in the same transaction as the chunk's data."""
    conn.execute(
        """
        INSERT INTO ingest_chunks
            (source, chunk_key, status, state, rows_in, rows_upserted, run_id, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (source, chunk_key) DO UPDATE SET
            status = excluded.status,
            state = excluded.state,
            rows_in = ingest_chunks.rows_in + excluded.rows_in,
            rows_upserted = ingest_chunks.rows_upserted + excluded.rows_upserted,
            run_id = excluded.run_id,
            updated_at = now()
        """,
        (source, key, status, Jsonb(dict(state)), rows_in, rows_upserted, run_id),
    )

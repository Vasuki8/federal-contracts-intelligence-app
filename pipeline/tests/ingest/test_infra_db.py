"""Archive, runs, budget and org upserts against the test database."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.archive import archive_bytes, archive_file, read_archived
from pipeline.ingest.budget import BudgetExhausted, DailyBudget, requests_today
from pipeline.ingest.context import ingest_run
from pipeline.ingest.orgs import OrgPath, upsert_org_path
from pipeline.ingest.runs import (
    JobAlreadyRunning,
    get_chunk,
    job_lock,
    last_success_param,
    save_chunk,
    start_run,
)
from pipeline.tests.helpers import count

pytestmark = pytest.mark.db
Conn = psycopg.Connection[TupleRow]


def test_archive_bytes_compresses_and_records_sha_of_original(conn: Conn, tmp_path: Path) -> None:
    content = b'{"totalRecords": 0}'
    archived = archive_bytes(
        conn,
        root=tmp_path,
        source="sam_opportunities",
        content=content,
        suffix=".json",
        run_id=None,
        http_status=200,
        request_params={"params": {"api_key": "secret", "limit": 1}},
        compress=True,
        fetched_at=datetime(2026, 10, 6, 12, 30, tzinfo=UTC),
    )
    assert archived.sha256 == hashlib.sha256(content).hexdigest()
    assert archived.path.startswith("sam_opportunities/2026/10/06/123000")
    assert archived.path.endswith(".json.gz")
    assert read_archived(tmp_path, archived.path) == content
    row = conn.execute("SELECT request_params, bytes FROM raw_files").fetchone()
    assert row is not None
    assert "secret" not in str(row[0])
    assert row[1] == len(content)


def test_archive_file_moves_download_into_archive(conn: Conn, tmp_path: Path) -> None:
    download = tmp_path / "download.zip"
    download.write_bytes(b"PK fake zip")
    archived = archive_file(
        conn,
        root=tmp_path / "raw",
        source="usaspending_awards",
        file=download,
        suffix=".zip",
        run_id=None,
        http_status=200,
        request_params={},
    )
    assert not download.exists()
    assert (tmp_path / "raw" / archived.path).read_bytes() == b"PK fake zip"


def test_ingest_run_records_success_partial_and_failure(db_url: str, tmp_path: Path) -> None:
    logs: list[str] = []
    with ingest_run(db_url, tmp_path, "src", "delta", {"until": "2026-10-06"}, logs.append):
        pass
    with ingest_run(db_url, tmp_path, "src", "delta", {}, logs.append):
        raise BudgetExhausted("src", 10)
    with (
        pytest.raises(ValueError, match="bad"),
        ingest_run(db_url, tmp_path, "src", "delta", {}, logs.append),
    ):
        raise ValueError("bad")
    with psycopg.connect(db_url) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM ingest_runs ORDER BY id")]
        assert statuses == ["succeeded", "partial", "failed"]
        assert last_success_param(conn, "src", "delta", "until") == "2026-10-06"
    assert any("budget" in line for line in logs)


def test_job_lock_blocks_a_second_concurrent_run(db_url: str) -> None:
    with (
        psycopg.connect(db_url, autocommit=True) as a,
        psycopg.connect(db_url, autocommit=True) as b,
    ):
        with (
            job_lock(a, "src", "backfill"),
            pytest.raises(JobAlreadyRunning),
            job_lock(b, "src", "backfill"),
        ):
            pass
        # Released once the first holder exits.
        with job_lock(b, "src", "backfill"):
            pass


def test_chunks_accumulate_rows_and_track_status(conn: Conn) -> None:
    run = start_run(conn, "src", "backfill", {})
    save_chunk(
        conn,
        source="src",
        key="k",
        status="in_progress",
        state={"offset": 1},
        run_id=run.id,
        rows_in=5,
    )
    save_chunk(conn, source="src", key="k", status="done", state={}, run_id=run.id, rows_in=3)
    chunk = get_chunk(conn, "src", "k")
    assert chunk is not None
    assert chunk.status == "done"
    row = conn.execute("SELECT rows_in FROM ingest_chunks").fetchone()
    assert row == (8,)
    assert get_chunk(conn, "src", "missing") is None


def test_budget_counts_only_todays_requests(conn: Conn, tmp_path: Path) -> None:
    now = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
    for when in (now - timedelta(days=1), now - timedelta(hours=1), now):
        archive_bytes(
            conn,
            root=tmp_path,
            source="sam",
            content=b"x",
            suffix=".json",
            run_id=None,
            http_status=200,
            request_params={},
            fetched_at=when,
        )
    assert requests_today(conn, "sam", now) == 2
    budget = DailyBudget(conn, "sam", limit=3, now=lambda: now)
    assert budget.remaining() == 1
    budget.check()
    tight = DailyBudget(conn, "sam", limit=2, now=lambda: now)
    with pytest.raises(BudgetExhausted):
        tight.check()


def test_org_upsert_is_idempotent_and_keeps_known_names(conn: Conn) -> None:
    path = OrgPath("047", "GSA", "4732", "FAS", "47QTCA", "IT CENTER")
    first = upsert_org_path(conn, path)
    again = upsert_org_path(conn, OrgPath("047", None, "4732", None, "47QTCA", None))
    assert first == again
    assert count(conn, "agencies") == 2
    assert count(conn, "offices") == 1
    row = conn.execute("SELECT name FROM offices").fetchone()
    assert row == ("IT CENTER",)
    assert upsert_org_path(conn, OrgPath("047", "GSA")) is None


def txn_status(conn: Conn) -> psycopg.pq.TransactionStatus:
    return conn.info.transaction_status


def test_keepalive_pings_idle_connections_without_losing_work(db_url: str, tmp_path: Path) -> None:
    now = [0.0]
    with ingest_run(db_url, tmp_path, "src", "x", {}, lambda _: None) as ctx:
        ctx.clock = lambda: now[0]
        ctx._last_ping = 0.0
        ctx.data.execute("INSERT INTO naics (code) VALUES ('111111')")  # uncommitted work
        now[0] = 61.0
        ctx.keepalive()
        assert txn_status(ctx.data) == psycopg.pq.TransactionStatus.INTRANS
        ctx.data.commit()
        now[0] = 200.0
        ctx.keepalive()  # leaves no empty transaction open behind the ping
        assert txn_status(ctx.data) == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(db_url) as check:
        assert check.execute("SELECT 1 FROM naics WHERE code = '111111'").fetchone()


def test_runs_left_running_are_marked_failed_by_the_next_run(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    start_run(conn, "src", "backfill", {})  # a run whose process died mid-way
    logs: list[str] = []
    with ingest_run(db_url, tmp_path, "src", "backfill", {}, logs.append):
        pass
    rows = conn.execute("SELECT status, error FROM ingest_runs ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["failed", "succeeded"]
    assert rows[0][1].startswith("Interrupted")
    assert any("Marked 1 earlier" in line for line in logs)


def test_a_failure_is_recorded_even_when_the_connections_died(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    with (
        pytest.raises(RuntimeError, match="database had gone"),
        ingest_run(db_url, tmp_path, "src", "backfill", {}, lambda _: None) as ctx,
    ):
        ctx.meta.close()
        ctx.data.close()
        raise RuntimeError("the file was ready, but the database had gone")
    row = conn.execute("SELECT status, error FROM ingest_runs").fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert "RuntimeError" in row[1]

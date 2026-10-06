"""USAspending awards: parser, store and job, with a fake USAspending API (no network)."""

import io
import json
import zipfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.archive import archive_bytes
from pipeline.ingest.awards import SOURCE
from pipeline.ingest.awards.client import build_client
from pipeline.ingest.awards.job import AwardsJob, Window, fiscal_year_windows, quarters
from pipeline.ingest.awards.parser import ALL_COLUMNS, read_award_csv, read_award_zip
from pipeline.ingest.awards.store import load_awards
from pipeline.ingest.context import IngestContext, ingest_run
from pipeline.tests.helpers import FIXTURES, count

US = FIXTURES / "usaspending"
FY2025 = US / "contracts_prime_award_summaries_fy2025.csv"
STALE = US / "contracts_prime_award_summaries_fy2024_stale.csv"
VERTICAL = frozenset({"541512", "541611"})
Conn = psycopg.Connection[TupleRow]
TASK_KEY = "CONT_AWD_47QTCA20F0001_4732_47QTCA19D0001_4732"


def zip_bytes(*csvs: Path, extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i, path in enumerate(csvs, 1):
            archive.writestr(f"All_Contracts_PrimeAwardSummaries_{i}.csv", path.read_text())
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return buffer.getvalue()


# --- parser ----------------------------------------------------------------


def test_fixture_header_is_the_real_286_column_award_layout() -> None:
    columns = (US / "award_d1_columns.txt").read_text().split()
    assert len(columns) == 286
    assert set(ALL_COLUMNS) <= set(columns)


def test_read_award_csv_selects_known_columns_as_text() -> None:
    frame = read_award_csv(FY2025)
    assert frame.frame.height == 3
    assert frame.frame.columns == list(ALL_COLUMNS)
    assert frame.missing_columns == ()
    assert set(frame.frame.dtypes) == {frame.frame.dtypes[0]}  # all String


def test_read_award_zip_skips_non_award_files(tmp_path: Path) -> None:
    path = tmp_path / "d.zip"
    path.write_bytes(zip_bytes(FY2025, extra={"README.csv": "a,b\n1,2\n", "notes.txt": "x"}))
    frames = read_award_zip(path, tmp_path / "work")
    assert [f.frame.height for f in frames] == [3]


def test_missing_columns_are_reported_and_filled(tmp_path: Path) -> None:
    path = tmp_path / "slim.csv"
    path.write_text("contract_award_unique_key,award_id_piid\nK1,P1\n")
    frame = read_award_csv(path)
    assert "naics_code" in frame.missing_columns
    assert frame.frame["naics_code"].to_list() == [None]


# --- store -----------------------------------------------------------------


@pytest.mark.db
def test_load_awards_casts_links_and_is_idempotent(conn: Conn) -> None:
    rows = read_award_csv(FY2025).frame
    first = load_awards(conn, rows, None, VERTICAL)
    assert (first.rows_in, first.awards_upserted, first.entities_upserted) == (3, 3, 2)
    load_awards(conn, rows, None, VERTICAL)
    assert {t: count(conn, t) for t in ("awards", "entities", "offices", "agencies", "naics")} == {
        "awards": 3,
        "entities": 2,
        "offices": 2,
        "agencies": 4,
        "naics": 2,
    }
    task = conn.execute(
        """
        SELECT piid_norm, referenced_idv_piid, solicitation_id_norm, obligated_total,
               number_of_offers, ultimate_end, office_id IS NOT NULL, set_aside_code
        FROM awards WHERE award_key = %s
        """,
        (TASK_KEY,),
    ).fetchone()
    assert task == (
        "47QTCA20F0001",
        "47QTCA19D0001",
        "47QTCA20Q0007",
        Decimal("1250000.50"),
        3,
        date(2027, 6, 30),
        True,
        "SBA",
    )
    # Malformed values become NULL instead of failing the load.
    bad = conn.execute(
        "SELECT obligated_total, ultimate_end, number_of_offers FROM awards"
        " WHERE piid = 'W91QUZ21C0001'"
    ).fetchone()
    assert bad == (None, None, None)
    types = conn.execute(
        "SELECT business_types FROM entities WHERE uei = 'SYNTHUEI0001'"
    ).fetchone()
    assert types is not None
    assert types[0]["c8a_program_participant"] is True
    assert types[0]["historically_underutilized_business_zone_hubzone_firm"] is False
    assert types[0]["contracting_officers_determination_of_business_size"] == "SMALL BUSINESS"
    naics = conn.execute("SELECT title, in_vertical FROM naics WHERE code = '541512'").fetchone()
    assert naics == ("COMPUTER SYSTEMS DESIGN SERVICES", True)


def row_versions(conn: Conn, table: str, key: str) -> list[tuple[Any, ...]]:
    """xmin changes whenever Postgres rewrites a row (each statement commits on its own)."""
    return conn.execute(f"SELECT {key}, xmin::text FROM {table} ORDER BY 1").fetchall()


@pytest.mark.db
def test_reloading_unchanged_rows_rewrites_nothing(conn: Conn, tmp_path: Path) -> None:
    rows = read_award_csv(FY2025).frame
    load_awards(conn, rows, None, VERTICAL)
    awards = row_versions(conn, "awards", "award_key")
    entities = row_versions(conn, "entities", "uei")
    # The same rows in a new download (another raw file) are still unchanged.
    file = archive_bytes(
        conn,
        root=tmp_path,
        source=SOURCE,
        content=b"PK",
        suffix=".zip",
        run_id=None,
        http_status=200,
        request_params={},
    )
    again = load_awards(conn, rows, file.id, VERTICAL)
    assert (again.awards_upserted, again.entities_upserted) == (0, 0)
    assert row_versions(conn, "awards", "award_key") == awards
    assert row_versions(conn, "entities", "uei") == entities


@pytest.mark.db
def test_changed_content_with_the_same_timestamp_is_updated(conn: Conn) -> None:
    rows = read_award_csv(FY2025).frame
    load_awards(conn, rows, None, VERTICAL)
    column = "prime_award_base_transaction_description"
    edited = rows.with_columns(
        pl.when(pl.col("contract_award_unique_key") == TASK_KEY)
        .then(pl.lit("CLOUD HOSTING, REVISED"))
        .otherwise(pl.col(column))
        .alias(column)
    )
    assert load_awards(conn, edited, None, VERTICAL).awards_upserted == 1
    row = conn.execute("SELECT description FROM awards WHERE award_key = %s", (TASK_KEY,))
    assert row.fetchone() == ("CLOUD HOSTING, REVISED",)


@pytest.mark.db
def test_older_rows_never_overwrite_newer_ones(conn: Conn) -> None:
    load_awards(conn, read_award_csv(FY2025).frame, None, VERTICAL)
    load_awards(conn, read_award_csv(STALE).frame, None, VERTICAL)
    row = conn.execute(
        "SELECT obligated_total FROM awards WHERE award_key = %s", (TASK_KEY,)
    ).fetchone()
    assert row == (Decimal("1250000.50"),)


# --- job -------------------------------------------------------------------


class FakeUsaspending:
    """POST download → file name; status: running then finished; GET file → zip."""

    def __init__(self, respond: Callable[[dict[str, Any]], tuple[bytes, int]]) -> None:
        self.respond = respond
        self.posts: list[dict[str, Any]] = []
        self.status_calls: dict[str, int] = {}
        self.files: dict[str, tuple[bytes, int]] = {}
        self.fail_status = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v2/download/awards/":
            body = json.loads(request.content)
            self.posts.append(body)
            name = f"file{len(self.posts)}.zip"
            self.files[name] = self.respond(body)
            return httpx.Response(200, json={"file_name": name, "file_url": f"/files/{name}"})
        if path == "/api/v2/download/status":
            if self.fail_status:
                return httpx.Response(503)
            name = request.url.params["file_name"]
            self.status_calls[name] = self.status_calls.get(name, 0) + 1
            if self.status_calls[name] == 1:
                return httpx.Response(200, json={"status": "running", "file_name": name})
            rows = self.files[name][1]
            return httpx.Response(
                200,
                json={
                    "status": "finished",
                    "file_name": name,
                    "file_url": f"/files/{name}",
                    "total_rows": rows,
                },
            )
        if path.startswith("/files/"):
            return httpx.Response(200, content=self.files[path.removeprefix("/files/")][0])
        return httpx.Response(404)


def run(
    db_url: str,
    tmp_path: Path,
    fake: FakeUsaspending,
    action: Callable[[AwardsJob], None],
    job: str,
) -> list[str]:
    logs: list[str] = []
    with ingest_run(db_url, tmp_path, SOURCE, job, {}, logs.append) as ctx:
        client, close = build_client(ctx, transport=httpx.MockTransport(fake), sleep=lambda _: None)
        try:
            action(AwardsJob(ctx, client, VERTICAL))
        finally:
            close()
    return logs


def periods(fake: FakeUsaspending) -> list[tuple[str, str, str]]:
    out = []
    for body in fake.posts:
        period = body["filters"]["time_period"][0]
        codes = "+".join(body["filters"]["naics_codes"]["require"])
        out.append((period["start_date"], period["end_date"], codes))
    return out


@pytest.mark.db
def test_backfill_by_fiscal_year_then_rerun_is_a_no_op(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    fake = FakeUsaspending(lambda body: (zip_bytes(FY2025), 3))
    today = date(2026, 10, 6)
    run(db_url, tmp_path, fake, lambda job: job.backfill(1, today), "backfill")
    assert periods(fake) == [
        ("2025-10-06", "2026-09-30", "541512+541611"),
        ("2026-10-01", "2026-10-06", "541512+541611"),
    ]
    body = fake.posts[0]
    assert body["filters"]["time_period"][0]["date_type"] == "action_date"
    assert "IDV_B" in body["filters"]["award_type_codes"]
    assert count(conn, "awards") == 3
    zips = conn.execute("SELECT count(*) FROM raw_files WHERE path LIKE '%.zip'").fetchone()
    assert zips == (2,)

    run(db_url, tmp_path, fake, lambda job: job.backfill(1, today), "backfill")
    assert len(fake.posts) == 2  # both chunks done: nothing requested again
    assert count(conn, "awards") == 3


@pytest.mark.db
def test_row_cap_splits_by_naics_code(db_url: str, conn: Conn, tmp_path: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[bytes, int]:
        multi = len(body["filters"]["naics_codes"]["require"]) > 1
        return zip_bytes(FY2025), 500_000 if multi else 3

    fake = FakeUsaspending(respond)
    run(db_url, tmp_path, fake, lambda job: job.backfill(1, date(2026, 9, 30)), "backfill")
    # 2025-09-30 is the last day of FY2025, so there are two fiscal-year chunks; each one
    # comes back over the cap and is split into one download per NAICS code.
    assert [p[2] for p in periods(fake)] == ["541512+541611", "541512", "541611"] * 2
    parent = conn.execute(
        "SELECT status, state ->> 'split' FROM ingest_chunks WHERE chunk_key LIKE '%541512+541611'"
    ).fetchone()
    assert parent == ("done", "true")
    assert count(conn, "awards") == 3


@pytest.mark.db
def test_delta_uses_last_modified_date_from_the_watermark(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    fake = FakeUsaspending(lambda body: (zip_bytes(FY2025), 3))
    run(db_url, tmp_path, fake, lambda job: job.delta(date(2026, 10, 6)), "delta")
    run(db_url, tmp_path, fake, lambda job: job.delta(date(2026, 10, 8)), "delta")
    first, second = (b["filters"]["time_period"][0] for b in fake.posts)
    assert first == {
        "start_date": "2026-10-03",
        "end_date": "2026-10-06",
        "date_type": "last_modified_date",
    }
    assert second["start_date"] == "2026-10-03"
    assert count(conn, "ingest_chunks") == 0


@pytest.mark.db
def test_a_crashed_run_resumes_its_pending_download(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    fake = FakeUsaspending(lambda body: (zip_bytes(FY2025), 3))
    fake.fail_status = True
    with pytest.raises(Exception, match="503"):
        run(db_url, tmp_path, fake, lambda job: job.backfill(1, date(2026, 9, 30)), "backfill")
    pending = conn.execute("SELECT status, state ->> 'file_name' FROM ingest_chunks").fetchone()
    assert pending == ("in_progress", "file1.zip")

    fake.fail_status = False
    run(db_url, tmp_path, fake, lambda job: job.backfill(1, date(2026, 9, 30)), "backfill")
    # The FY2025 tail chunk was already requested: it is polled, not requested again.
    # (The second POST is for the FY2026 chunk, which the crashed run never reached.)
    assert [p[:2] for p in periods(fake)] == [
        ("2025-09-30", "2025-09-30"),
        ("2025-10-01", "2026-09-30"),
    ]
    assert fake.status_calls["file1.zip"] == 2
    assert count(conn, "awards") == 3
    statuses = [r[0] for r in conn.execute("SELECT status FROM ingest_runs ORDER BY id")]
    assert statuses == ["failed", "succeeded"]


def test_window_helpers() -> None:
    fys = fiscal_year_windows(date(2021, 10, 6), date(2026, 10, 6))
    assert fys[0] == Window(date(2021, 10, 6), date(2022, 9, 30))
    assert fys[-1] == Window(date(2026, 10, 1), date(2026, 10, 6))
    assert len(fys) == 6
    parts = quarters(Window(date(2025, 10, 1), date(2026, 9, 30)))
    assert len(parts) == 4
    assert parts[0].start == date(2025, 10, 1) and parts[-1].end == date(2026, 9, 30)


@pytest.mark.db
def test_long_waits_keep_the_database_connections_alive(
    db_url: str, conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(IngestContext, "keepalive", lambda self: calls.append(1))
    fake = FakeUsaspending(lambda body: (zip_bytes(FY2025), 3))
    run(db_url, tmp_path, fake, lambda job: job.backfill(1, date(2026, 9, 30)), "backfill")
    # Each chunk polls once while "running", streams the file, then loads it.
    assert len(calls) >= 6

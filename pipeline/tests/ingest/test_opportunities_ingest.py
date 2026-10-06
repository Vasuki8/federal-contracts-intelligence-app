"""Notice store and job tests against the test DB, with a fake SAM API (no network)."""

import copy
import itertools
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.context import ingest_run
from pipeline.ingest.opportunities import SOURCE
from pipeline.ingest.opportunities.client import build_client
from pipeline.ingest.opportunities.job import OpportunitiesJob, windows
from pipeline.ingest.opportunities.parser import parse_page
from pipeline.ingest.opportunities.store import upsert_notices
from pipeline.tests.helpers import FIXTURES, count

pytestmark = pytest.mark.db
Conn = psycopg.Connection[TupleRow]
KEY = "test-sam-key-do-not-store"
SAM = FIXTURES / "sam_opportunities"
VERTICAL = frozenset({"541511", "541512"})


def page(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAM / name).read_text())
    return data


def records(n: int, naics: str, prefix: str) -> list[dict[str, Any]]:
    base = page("amendment_v1.json")["opportunitiesData"][0]
    out = []
    for i in range(n):
        record = copy.deepcopy(base)
        record.update({"noticeId": f"{prefix}{i}", "naicsCode": naics})
        out.append(record)
    return out


class FakeSam:
    """Serves pages keyed by (ncode, offset); anything else is the docs' 404 'No Data found'."""

    def __init__(self, pages: dict[tuple[str | None, int], dict[str, Any]]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []
        self.status_override: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override)
        key = (request.url.params.get("ncode"), int(request.url.params["offset"]))
        if key not in self.pages:
            return httpx.Response(404, text="No Data found")
        return httpx.Response(200, json=self.pages[key])


def run_job(
    db_url: str,
    tmp_path: Path,
    fake: FakeSam,
    action: Callable[[OpportunitiesJob], None],
    job: str = "backfill",
    limit: int = 100,
) -> list[str]:
    logs: list[str] = []
    transport = httpx.MockTransport(fake)
    with (
        ingest_run(db_url, tmp_path, SOURCE, job, {}, logs.append) as ctx,
        build_client(ctx, KEY, limit, transport=transport, sleep=lambda _: None) as client,
    ):
        action(OpportunitiesJob(ctx, client, VERTICAL))
    return logs


def run_statuses(conn: Conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM ingest_runs ORDER BY id")]


# --- store -----------------------------------------------------------------


def test_amendment_creates_version_2_with_a_diff(conn: Conn) -> None:
    v1 = parse_page(page("amendment_v1.json")).notices
    v2 = parse_page(page("amendment_v2.json")).notices
    first = upsert_notices(conn, v1, None)
    repeat = upsert_notices(conn, v1, None)
    amended = upsert_notices(conn, v2, None)
    assert (first.inserted, repeat.unchanged, amended.changed) == (1, 1, 1)
    assert count(conn, "notices") == 1
    assert count(conn, "notice_versions") == 2
    row = conn.execute(
        "SELECT diff, version FROM notice_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    diff, version = row
    assert version == 2
    assert set(diff) == {"title", "response_deadline", "posted_at", "attachment_links"}
    assert diff["title"][1] == "IT Help Desk Support Services (Amendment 1)"
    notice = conn.execute(
        "SELECT latest_version, solicitation_number_norm, office_code, set_aside_code FROM notices"
    ).fetchone()
    assert notice == (2, "47QTCA26R0001", "47QTCA", "SBA")
    assert count(conn, "offices") == 1
    assert count(conn, "agencies") == 2


# --- job -------------------------------------------------------------------


def test_backfill_pages_filters_and_is_idempotent(db_url: str, conn: Conn, tmp_path: Path) -> None:
    first_page = {"totalRecords": 1001, "opportunitiesData": records(1000, "541512", "a")}
    second_page = {"totalRecords": 1001, "opportunitiesData": records(1, "541512", "b")}
    other = {
        "totalRecords": 2,
        "opportunitiesData": records(1, "541511", "c") + records(1, "999999", "d"),
    }
    fake = FakeSam({("541512", 0): first_page, ("541512", 1): second_page, ("541511", 0): other})
    since, until = date(2026, 1, 1), date(2026, 3, 31)

    run_job(db_url, tmp_path, fake, lambda job: job.backfill(since, until))
    assert len(fake.requests) == 3
    assert count(conn, "notices") == 1002  # the non-vertical record is dropped
    assert count(conn, "notice_versions") == 1002
    assert count(conn, "raw_files") == 3
    params = fake.requests[0].url.params
    assert (params["postedFrom"], params["postedTo"], params["limit"]) == (
        "01/01/2026",
        "03/31/2026",
        "1000",
    )

    # Re-running a finished backfill makes no requests and adds no rows.
    run_job(db_url, tmp_path, fake, lambda job: job.backfill(since, until))
    assert len(fake.requests) == 3
    assert count(conn, "notices") == 1002
    assert count(conn, "notice_versions") == 1002
    assert run_statuses(conn) == ["succeeded", "succeeded"]


def test_budget_exhaustion_is_partial_and_the_next_run_resumes(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    pages: dict[tuple[str | None, int], dict[str, Any]] = {
        ("541512", 0): {"totalRecords": 1001, "opportunitiesData": records(1000, "541512", "a")},
        ("541512", 1): {"totalRecords": 1001, "opportunitiesData": records(1, "541512", "b")},
    }
    fake = FakeSam(pages)
    since, until = date(2026, 1, 1), date(2026, 1, 31)

    # Codes run in sorted order: 541511 (no data, a 404) then 541512 page 0, then the
    # budget of 2 is spent before page 1.
    logs = run_job(db_url, tmp_path, fake, lambda job: job.backfill(since, until), limit=2)
    assert len(fake.requests) == 2
    assert run_statuses(conn) == ["partial"]
    assert any("budget" in line for line in logs)
    assert count(conn, "notices") == 1000

    run_job(db_url, tmp_path, fake, lambda job: job.backfill(since, until), limit=10)
    resumed = [(r.url.params.get("ncode"), r.url.params["offset"]) for r in fake.requests[2:]]
    assert resumed == [("541512", "1")]  # finished chunks are skipped; resumes at the next page
    assert count(conn, "notices") == 1001
    assert run_statuses(conn) == ["partial", "succeeded"]


def test_sam_429_ends_the_run_as_partial(db_url: str, conn: Conn, tmp_path: Path) -> None:
    fake = FakeSam({})
    fake.status_override = 429
    run_job(db_url, tmp_path, fake, lambda job: job.backfill(date(2026, 1, 1), date(2026, 1, 2)))
    assert run_statuses(conn) == ["partial"]
    assert len(fake.requests) == 3  # retried, then stopped cleanly


def test_delta_queries_all_naics_once_and_filters_locally(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    data = records(2, "541511", "v") + records(3, "236220", "x")
    fake = FakeSam({(None, 0): {"totalRecords": 5, "opportunitiesData": data}})
    run_job(db_url, tmp_path, fake, lambda job: job.delta(date(2026, 10, 6)), job="delta")
    params = fake.requests[0].url.params
    assert "ncode" not in params
    assert (params["postedFrom"], params["postedTo"]) == ("10/04/2026", "10/06/2026")
    assert count(conn, "notices") == 2

    # The next delta starts from the last successful window end, minus the overlap.
    run_job(db_url, tmp_path, fake, lambda job: job.delta(date(2026, 10, 9)), job="delta")
    assert fake.requests[1].url.params["postedFrom"] == "10/04/2026"
    assert count(conn, "notices") == 2


def test_api_key_is_never_stored(db_url: str, conn: Conn, tmp_path: Path) -> None:
    fake = FakeSam({(None, 0): page("amendment_v1.json")})
    run_job(db_url, tmp_path, fake, lambda job: job.delta(date(2026, 10, 6)), job="delta")
    assert fake.requests[0].url.params["api_key"] == KEY
    for table in ("raw_files", "ingest_runs", "notices", "notice_versions"):
        dump = conn.execute(
            f"SELECT coalesce(string_agg(t::text, ''), '') FROM {table} t"
        ).fetchone()
        assert dump is not None
        assert KEY not in dump[0], table
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


def test_windows_never_exceed_one_year() -> None:
    parts = windows(date(2024, 1, 1), date(2026, 6, 30))
    assert parts[0].start == date(2024, 1, 1)
    assert parts[-1].end == date(2026, 6, 30)
    assert all((w.end - w.start).days < 365 for w in parts)
    assert all(b.start > a.end for a, b in itertools.pairwise(parts))


def test_backfill_resumes_across_days_with_the_original_dates(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    pages: dict[tuple[str | None, int], dict[str, Any]] = {
        ("541511", 0): {"totalRecords": 1, "opportunitiesData": records(1, "541511", "a")},
        ("541512", 0): {"totalRecords": 1, "opportunitiesData": records(1, "541512", "b")},
    }
    fake = FakeSam(pages)
    day1, day2, day3 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)

    # Day 1: a new 12-month plan (one 365-day window, so one query per code); the
    # budget (1 request) covers only the first code.
    logs = run_job(db_url, tmp_path, fake, lambda job: job.continue_backfill(day1), limit=1)
    assert any("Starting backfill 2025-10-07..2026-10-06" in line for line in logs)
    assert run_statuses(conn) == ["partial"]

    # Day 2: resumes the same plan; the dates do not move with the calendar.
    logs = run_job(db_url, tmp_path, fake, lambda job: job.continue_backfill(day2), limit=10)
    assert any("Resuming backfill 2025-10-07..2026-10-06: 1 of 2" in line for line in logs)
    second = fake.requests[1].url.params
    assert (second["ncode"], second["postedTo"]) == ("541512", "10/06/2026")
    assert count(conn, "notices") == 2

    # Day 3: the plan is complete, so nothing is fetched.
    logs = run_job(db_url, tmp_path, fake, lambda job: job.continue_backfill(day3), limit=10)
    assert len(fake.requests) == 2
    assert any("is complete" in line for line in logs)

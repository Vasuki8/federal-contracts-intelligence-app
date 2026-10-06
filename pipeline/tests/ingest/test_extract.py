"""SAM.gov CSV extracts: parsing real rows, the load job, and merging with API notices."""

import csv
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.archive import read_archived
from pipeline.ingest.context import ingest_run
from pipeline.ingest.opportunities.extract import (
    COLUMNS,
    DAILY_URL,
    ExtractFormatError,
    archive_url,
    as_record,
    check_header,
    parse_row,
    read_rows,
)
from pipeline.ingest.opportunities.extract_job import ExtractJob
from pipeline.ingest.opportunities.parser import parse_page
from pipeline.ingest.opportunities.store import upsert_notices
from pipeline.tests.helpers import FIXTURES, count

Conn = psycopg.Connection[TupleRow]
LIVE = FIXTURES / "sam_extract" / "live_sample.csv"
VERTICAL = frozenset({"541330", "541511", "541512", "541519"})


def csv_bytes(*rows: dict[str, str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row.get(column, "") for column in COLUMNS])
    return buffer.getvalue().encode("cp1252")


# --- parsing ------------------------------------------------------------------


def test_live_rows_parse_with_their_encoding_codes_and_awards() -> None:
    header, rows = read_rows(LIVE)
    assert header.unknown == ()
    records = [as_record(header, row) for row in rows]
    assert len(records) == 7
    items = {item.notice.notice_id: item for r in records if (item := parse_row(r))}

    first = items["8246dc8caad6468b90d37254bf50edcf"].notice
    assert "\u2013 DCF Mail" in (first.title or "")  # Windows-1252 en dash decoded
    assert first.full_parent_path_code == "020.2041.2031ZA"
    assert (first.type, first.base_type, first.active) == ("Special Notice", "Sources Sought", True)
    assert first.response_deadline_has_time is True
    assert first.attachment_links is None  # unknown in the CSV
    assert first.contacts and first.contacts[0]["type"] == "primary"

    award = items["370528b663f947828d4b012deaa1e72b"]
    assert award.notice.active is False
    assert award.notice.award == {
        "number": "693JJ126F00102N",
        "date": "2026-09-28",
        "amount": "2118578.88",
        "awardee": {"name": "ERNST & YOUNG LLP New York NY 10001 USA"},
    }
    assert award.description is not None


def test_a_changed_header_stops_the_load(tmp_path: Path) -> None:
    with pytest.raises(ExtractFormatError, match="Description"):
        check_header([c for c in COLUMNS if c != "Description"])
    extra = check_header([*COLUMNS, "NewColumn"])
    assert extra.unknown == ("NewColumn",)
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(ExtractFormatError, match="empty"):
        read_rows(empty)


# --- job ----------------------------------------------------------------------


def serve(files: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"etag": '"abc-26"'})

    return httpx.Client(transport=httpx.MockTransport(handler))


def load(
    db_url: str, tmp_path: Path, files: dict[str, bytes], run: Callable[[ExtractJob], None]
) -> list[str]:
    logs: list[str] = []
    with ingest_run(db_url, tmp_path, "sam_extract", "daily", {}, logs.append) as ctx:
        run(ExtractJob(ctx, serve(files), VERTICAL, sleep=lambda _: None))
    return logs


@pytest.mark.db
def test_daily_extract_keeps_vertical_rows_and_archives_them(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    files = {DAILY_URL: LIVE.read_bytes()}
    logs = load(db_url, tmp_path, files, lambda job: job.daily())
    assert any("7 rows in the file, 4 in the vertical: 4 new" in line for line in logs)
    assert count(conn, "notices") == 4
    assert count(conn, "notice_descriptions") == 4
    row = conn.execute("SELECT path, request_params FROM raw_files").fetchone()
    assert row is not None
    path, params = row
    assert params["rows_in_file"] == 7 and params["rows_kept"] == 4
    assert params["etag"] == '"abc-26"' and len(params["file_sha256"]) == 64
    archived = list(csv.reader(io.StringIO(read_archived(tmp_path, path).decode())))
    assert archived[0] == list(COLUMNS) and len(archived) == 5

    again = load(db_url, tmp_path, files, lambda job: job.daily())
    assert any("already loaded" in line for line in again)
    assert count(conn, "raw_files") == 1
    forced = load(db_url, tmp_path, files, lambda job: job.daily(force=True))
    assert any("0 new, 0 amended, 0 updated, 4 unchanged" in line for line in forced)


@pytest.mark.db
def test_archive_extract_can_limit_by_posted_date(db_url: str, conn: Conn, tmp_path: Path) -> None:
    from datetime import date

    files = {archive_url(2026): LIVE.read_bytes()}
    load(db_url, tmp_path, files, lambda job: job.archive(2026, since=date(2026, 9, 30)))
    assert count(conn, "notices") == 1  # only the vertical row posted on or after 2026-09-30


# --- merging with API notices ---------------------------------------------------


def api_notice() -> Any:
    payload = json.loads((FIXTURES / "sam_opportunities" / "amendment_v1.json").read_text())
    return parse_page(payload).notices


CSV_ROW = {
    "NoticeId": "synthetic0000000000000000amend001",
    "Title": "IT Help Desk Support Services",
    "Sol#": "47QTCA-26-R-0001",
    "CGAC": "047",
    "FPDS Code": "4732",
    "AAC Code": "47QTCA",
    "PostedDate": "2026-10-01 09:15:00",
    "Type": "Solicitation",
    "BaseType": "Solicitation",
    "ArchiveType": "autocustom",
    "ArchiveDate": "2026-12-31",
    "SetASideCode": "SBA",
    "ResponseDeadLine": "2026-11-02T14:00:00-05:00",
    "NaicsCode": "541512",
    "ClassificationCode": "D302",
    "Active": "Yes",
    "Description": "Follow-on to the incumbent contract W91QUZ-21-C-0001.",
}


def versions(conn: Conn) -> list[tuple[int, Any]]:
    return conn.execute("SELECT version, diff FROM notice_versions ORDER BY version").fetchall()


@pytest.mark.db
def test_csv_fills_in_the_description_without_a_new_version(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    upsert_notices(conn, api_notice(), None)
    load(db_url, tmp_path, {DAILY_URL: csv_bytes(CSV_ROW)}, lambda job: job.daily())
    assert versions(conn) == [(1, None)]
    notice = conn.execute("SELECT attachment_links, contacts FROM notices")
    links, contacts = notice.fetchone() or (None, None)
    assert links == ["https://example.invalid/attachments/statement-of-work.pdf"]  # kept
    assert contacts and contacts[0]["fullName"]  # the API's contacts are kept
    text = conn.execute("SELECT version, text FROM notice_descriptions").fetchall()
    assert text == [(1, CSV_ROW["Description"])]

    # The API seeing the same notice again changes nothing it doesn't own.
    assert upsert_notices(conn, api_notice(), None).written == 0


@pytest.mark.db
def test_a_real_change_in_the_csv_makes_a_new_version(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    upsert_notices(conn, api_notice(), None)
    load(db_url, tmp_path, {DAILY_URL: csv_bytes(CSV_ROW)}, lambda job: job.daily())
    amended = {**CSV_ROW, "Title": "IT Help Desk Support Services (Amendment 1)"}
    amended["Description"] = "Updated statement of work."
    load(db_url, tmp_path, {DAILY_URL: csv_bytes(amended)}, lambda job: job.daily())
    rows = versions(conn)
    assert [version for version, _ in rows] == [1, 2]
    assert set(rows[1][1]) == {"title", "description_sha256"}
    descriptions = conn.execute("SELECT version FROM notice_descriptions ORDER BY 1").fetchall()
    assert descriptions == [(1,), (2,)]


@pytest.mark.db
def test_a_date_only_deadline_does_not_undo_a_precise_one(conn: Conn) -> None:
    upsert_notices(conn, api_notice(), None)  # 2026-11-02T14:00:00-05:00
    [notice] = api_notice()
    date_only = notice.model_copy(
        update={
            "response_deadline": notice.response_deadline.replace(hour=0, minute=0)
            if notice.response_deadline
            else None,
            "response_deadline_has_time": False,
        }
    )
    result = upsert_notices(conn, [date_only], None)
    assert (result.changed, result.updated) == (0, 0)
    assert versions(conn) == [(1, None)]

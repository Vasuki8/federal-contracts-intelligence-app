"""Tests against real responses saved by the first live sample (2026-10-06, GitHub Actions).

These pin down what the docs left unclear; see docs/data-sources.md.
"""

import csv
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.awards.parser import read_award_csv
from pipeline.ingest.awards.store import load_awards
from pipeline.ingest.opportunities.parser import parse_page
from pipeline.ingest.opportunities.store import upsert_notices
from pipeline.ingest.orgs import parse_parent_path
from pipeline.tests.helpers import FIXTURES, count

SAM_LIVE = FIXTURES / "sam_opportunities" / "live_sample.json"
AWARDS_LIVE = FIXTURES / "usaspending" / "live_sample.csv"
Conn = psycopg.Connection[TupleRow]


def sam_page() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SAM_LIVE.read_text())
    return data


def test_live_sam_page_has_no_unexpected_fields() -> None:
    page = parse_page(sam_page())
    assert page.total_records == 5127
    assert len(page.notices) == 3
    assert page.unknown_fields == set()  # naicsCodes is known now (observed, not documented)


def test_live_deadlines_are_plain_dates() -> None:
    first = parse_page(sam_page()).notices[0]
    assert first.response_deadline == datetime(2026, 10, 13, tzinfo=UTC)
    assert first.response_deadline_has_time is False
    assert first.posted_at == datetime(2026, 10, 6, tzinfo=UTC)


def test_live_naics_codes_list() -> None:
    first = parse_page(sam_page()).notices[0]
    assert first.naics == "339112"
    assert first.naics_codes == ("339112",)
    assert first.in_naics(frozenset({"339112"}))
    assert not first.in_naics(frozenset({"541512"}))


def test_live_org_paths_put_the_office_code_last() -> None:
    path = parse_parent_path(
        "097.97AS.DLA LAND.DLA LAND COLUMBUS.SPE7L1",
        "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA LAND.DLA LAND COLUMBUS.DLA LAND AND MARITIME",
    )
    assert (path.department_code, path.subtier_code, path.office_code) == ("097", "97AS", "SPE7L1")
    assert path.subtier_name == "DEFENSE LOGISTICS AGENCY"
    assert path.office_name == "DLA LAND AND MARITIME"


@pytest.mark.db
def test_live_notices_store_office_codes_and_deadline_flags(conn: Conn) -> None:
    result = upsert_notices(conn, parse_page(sam_page()).notices, None)
    assert result.inserted == 3
    rows = conn.execute(
        "SELECT subtier_code, office_code, response_deadline_has_time, naics_codes FROM notices"
    ).fetchall()
    assert {r[0] for r in rows} == {"97AS"}
    assert {r[1] for r in rows} == {"SPE2DS", "SPE7L1", "SPE7M2"}
    assert all(r[2] is False for r in rows)
    assert all(len(r[3]) == 1 for r in rows)


def test_live_award_header_matches_the_documented_layout() -> None:
    with AWARDS_LIVE.open(newline="") as handle:
        header = next(csv.reader(handle))
    documented = (FIXTURES / "usaspending" / "award_d1_columns.txt").read_text().split()
    assert header == documented


@pytest.mark.db
def test_live_awards_load_with_every_date_and_flag_parsed(conn: Conn) -> None:
    frame = read_award_csv(AWARDS_LIVE)
    assert frame.missing_columns == ()
    result = load_awards(conn, frame.frame, None, frozenset({"541512"}))
    assert result.rows_in == 25
    assert count(conn, "awards") == 25
    # "YYYY-MM-DD 00:00:00" end dates and "…+00" timestamps must not be lost to NULL.
    with AWARDS_LIVE.open(newline="") as handle:
        with_end = sum(
            1 for r in csv.DictReader(handle) if r["period_of_performance_potential_end_date"]
        )
    row = conn.execute(
        "SELECT count(ultimate_end), count(last_modified), count(obligated_total) FROM awards"
    ).fetchone()
    assert row == (with_end, 25, 25)
    flags = conn.execute(
        "SELECT count(*) FROM entities WHERE business_types ? 'c8a_program_participant'"
    ).fetchone()
    assert flags is not None and flags[0] == count(conn, "entities")
    # Toptier agency codes can be 4 characters (e.g. "1100").
    assert conn.execute(
        "SELECT 1 FROM agencies WHERE level = 'department' AND code = '1100'"
    ).fetchone()

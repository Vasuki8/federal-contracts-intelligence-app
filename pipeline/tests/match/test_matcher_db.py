"""The matcher against the test database: candidates, explicit references, history
links, superseded awards, idempotent re-runs and human decisions."""

from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.match.diagnose import diagnose
from pipeline.match.reset import reset_automatic_matches
from pipeline.recompetes import refresh_recompetes
from pipeline.tests.match.conftest import CFG, TODAY, VERTICAL, run_matcher
from pipeline.tests.match.factories import add_award, add_notice

pytestmark = pytest.mark.db
Conn = psycopg.Connection[TupleRow]


def matches(conn: Conn, notice_id: str, kind: str = "incumbent") -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT award_key, method, score::float8, rank, shown, status, evidence
        FROM notice_award_matches WHERE notice_id = %s AND kind = %s
        """,
        (notice_id, kind),
    ).fetchall()
    keys = ("method", "score", "rank", "shown", "status", "evidence")
    return {row[0]: dict(zip(keys, row[1:], strict=True)) for row in rows}


def test_matcher_finds_incumbents_and_history(db_url: str, world: Conn, tmp_path: Path) -> None:
    summary = run_matcher(db_url, tmp_path)
    assert summary.notices == 2  # N1 and N2; OLD is inactive and AWD is an award notice

    n1 = matches(world, "N1")
    assert set(n1) == {"A1", "A2", "A4"}  # A3: other agency; 47QTCB22D0001: a vehicle
    assert n1["A1"]["shown"] == "incumbent" and n1["A1"]["rank"] == 1
    reasons = n1["A1"]["evidence"]["reasons"]
    assert reasons[0] == "Same contracting office (GSA/FAS IT CENTER)"
    assert "Contract value is in line with the amount in the notice" in reasons
    assert n1["A4"]["evidence"]["features"]["vehicle"] == 1.0  # ordered under the GWAC
    assert n1["A2"]["shown"] == "hidden"

    n2 = matches(world, "N2")
    assert n2["W91QUZ21C0001"]["method"] == "explicit_reference"
    assert n2["W91QUZ21C0001"]["shown"] == "incumbent"
    assert n2["W91QUZ21C0001"]["evidence"]["context_words"] == ["incumbent", "follow-on"]

    assert set(matches(world, "OLD", "resulting_award")) == {"WON"}
    assert matches(world, "AWD", "resulting_award")["47QTCA20F0001"]["method"] == (
        "award_notice_number"
    )


def test_rerun_writes_nothing_and_keeps_human_decisions(
    db_url: str, world: Conn, tmp_path: Path
) -> None:
    run_matcher(db_url, tmp_path)
    again = run_matcher(db_url, tmp_path)
    assert (again.written, again.deleted, again.history_written) == (0, 0, 0)

    world.execute(
        "UPDATE notice_award_matches SET status = 'rejected', shown = 'hidden' "
        "WHERE notice_id = 'N1' AND award_key = 'A1'"
    )
    run_matcher(db_url, tmp_path)
    n1 = matches(world, "N1")
    assert (n1["A1"]["status"], n1["A1"]["shown"]) == ("rejected", "hidden")
    assert n1["A4"]["shown"] == "possible"  # the runner-up, without A1 to beat it

    world.execute(
        "UPDATE notice_award_matches SET status = 'confirmed', shown = 'incumbent' "
        "WHERE notice_id = 'N1' AND award_key = 'A4'"
    )
    run_matcher(db_url, tmp_path)
    shown = {key: row["shown"] for key, row in matches(world, "N1").items()}
    assert shown == {"A1": "hidden", "A2": "hidden", "A4": "incumbent"}


def test_stale_automatic_matches_are_removed(db_url: str, world: Conn, tmp_path: Path) -> None:
    run_matcher(db_url, tmp_path)
    world.execute("UPDATE awards SET ultimate_end = '2020-01-01' WHERE award_key = 'A2'")
    summary = run_matcher(db_url, tmp_path)
    assert summary.deleted == 1
    assert "A2" not in matches(world, "N1")


def test_an_incumbent_already_replaced_is_not_a_candidate_again(
    db_url: str, world: Conn, tmp_path: Path
) -> None:
    run_matcher(db_url, tmp_path)
    # OLD already chose A1 as its incumbent and has been awarded (to WON).
    world.execute(
        """
        INSERT INTO notice_award_matches
            (notice_id, award_key, kind, method, score, shown, status, matcher_version)
        VALUES ('OLD', 'A1', 'incumbent', 'candidate', 0.9, 'incumbent', 'confirmed', 't')
        """
    )
    run_matcher(db_url, tmp_path)
    assert "A1" not in matches(world, "N1")


def test_recompetes_window_types_and_links(conn: Conn, db_url: str, tmp_path: Path) -> None:
    # TODAY = 2026-10-06, so the window is 2027-04-06 .. 2028-10-06.
    add_award(conn, "EDGE_IN", ultimate_end=date(2027, 4, 6))
    add_award(conn, "LATE_IN", ultimate_end=date(2028, 10, 6), recipient_name=None)
    add_award(conn, "TOO_SOON", ultimate_end=date(2027, 4, 5))
    add_award(conn, "TOO_LATE", ultimate_end=date(2028, 10, 7))
    add_award(conn, "VEHICLE", award_type_code="IDV_A", ultimate_end=date(2028, 1, 1))
    add_award(conn, "SMALL", total_value=1_000, ultimate_end=date(2028, 1, 1))
    add_award(conn, "OTHER_NAICS", naics="236220", ultimate_end=date(2028, 1, 1))
    add_award(
        conn,
        "IDC",
        award_type_code="IDV_B",
        ultimate_end=None,
        ordering_period_end=date(2028, 1, 1),
    )
    add_notice(conn, "N9")
    conn.execute(
        """
        INSERT INTO notice_award_matches
            (notice_id, award_key, kind, method, score, shown, matcher_version)
        VALUES ('N9', 'EDGE_IN', 'incumbent', 'candidate', 0.9, 'incumbent', 't')
        """
    )
    conn.execute("INSERT INTO entities (uei, name) VALUES ('UEI0000000A1', 'ACME IT LLC')")
    with psycopg.connect(db_url) as data:
        count = refresh_recompetes(data, VERTICAL, CFG, TODAY)
        data.commit()
        assert refresh_recompetes(data, VERTICAL, CFG, TODAY) == count  # rebuilt, not doubled
        data.commit()
    rows: dict[str, str | None] = dict(
        conn.execute("SELECT award_key, linked_notice_id FROM recompetes").fetchall()
    )
    assert rows == {"EDGE_IN": "N9", "LATE_IN": None, "IDC": None}
    name = conn.execute("SELECT recipient_name FROM recompetes WHERE award_key = 'LATE_IN'")
    assert name.fetchone() == ("ACME IT LLC",)


def test_diagnose_reports_inputs_and_results(db_url: str, world: Conn, tmp_path: Path) -> None:
    run_matcher(db_url, tmp_path)
    text = "\n".join(diagnose(world, VERTICAL, CFG))
    assert "Solicitation" in text and "archived" in text
    assert "office found in awards        2 of 2 (100%)" in text
    assert "active notices with a description  2 of 2 (100%)" in text
    assert "incumbent shown               2 of 2 (100%)" in text
    assert "cites a contract number       1 of 2 (50%)" in text
    assert "notices linked to the award made from them  2" in text


def test_placeholder_and_overly_shared_numbers_make_no_history_links(
    db_url: str, conn: Conn, tmp_path: Path
) -> None:
    add_notice(conn, "PLACEHOLDER", active=False, solicitation_number="N/A")
    add_notice(conn, "SHORT", active=False, solicitation_number="RFQ-0001")
    for i in range(3):
        add_award(conn, f"NA{i}", solicitation_id="N/A", ultimate_end=date(2031, 1, 1))
        add_award(conn, f"RFQ{i}", solicitation_id="RFQ0001", ultimate_end=date(2031, 1, 1))
    add_notice(conn, "IDIQ", active=False, solicitation_number="47QRAD-20-R-0001")
    for i in range(51):  # more awards than one notice can plausibly have produced
        add_award(conn, f"OASIS{i}", solicitation_id="47QRAD20R0001", ultimate_end=date(2031, 1, 1))
    add_notice(conn, "REAL", active=False, solicitation_number="47QTCA-25-Q-0099")
    add_award(conn, "WON", solicitation_id="47QTCA25Q0099", ultimate_end=date(2031, 1, 1))
    run_matcher(db_url, tmp_path)
    linked = conn.execute(
        "SELECT DISTINCT notice_id FROM notice_award_matches WHERE kind = 'resulting_award'"
    ).fetchall()
    assert linked == [("REAL",)]


def test_reset_frees_matches_but_keeps_human_decisions(
    db_url: str, world: Conn, tmp_path: Path
) -> None:
    run_matcher(db_url, tmp_path)
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert "no human decisions" in reset_automatic_matches(conn)
    assert world.execute("SELECT count(*) FROM notice_award_matches").fetchone() == (0,)

    run_matcher(db_url, tmp_path)
    world.execute("UPDATE notice_award_matches SET status = 'confirmed' WHERE award_key = 'A1'")
    world.execute(
        "INSERT INTO match_reviews (notice_id, award_key, decision) "
        "VALUES ('N1', 'A1', 'confirmed')"
    )
    with psycopg.connect(db_url, autocommit=True) as conn:
        message = reset_automatic_matches(conn)
    assert "kept 1 human decisions and 1 reviews" in message
    rows = world.execute("SELECT award_key, status FROM notice_award_matches").fetchall()
    assert rows == [("A1", "confirmed")]

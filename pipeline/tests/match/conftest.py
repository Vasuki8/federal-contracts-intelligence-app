"""Shared matcher test data: a GSA help-desk recompete, an Army notice that cites its
incumbent, and history (an awarded solicitation and an award notice)."""

from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.ingest.context import ingest_run
from pipeline.match.config import load_matching_config
from pipeline.match.job import MatchJob, MatchSummary
from pipeline.tests.match.factories import add_award, add_description, add_notice

Conn = psycopg.Connection[TupleRow]
CFG = load_matching_config()
VERTICAL = frozenset({"541512", "541519"})
TODAY = date(2026, 10, 6)


def run_matcher(db_url: str, tmp_path: Path) -> MatchSummary:
    with ingest_run(db_url, tmp_path, "matcher", "notices", {}, lambda _: None) as ctx:
        return MatchJob(ctx, CFG, VERTICAL, TODAY).run()


@pytest.fixture
def world(conn: Conn) -> Conn:
    """A help-desk recompete at GSA, an Army notice citing its incumbent, and history."""
    add_notice(conn, "N1", solicitation_number="47QTCA-26-Q-0007")
    add_award(conn, "A1", referenced_idv_piid="47QTCB22D0001")  # the incumbent
    add_award(
        conn,
        "A2",
        ultimate_end=date(2026, 7, 1),
        description="NETWORK EQUIPMENT",
        naics="541519",
        psc="7025",
    )
    add_award(conn, "A3", awarding_sub_agency_code="9700", awarding_office_code="X")
    add_award(conn, "47QTCB22D0001", award_type_code="IDV_A", ultimate_end=date(2030, 1, 1))
    add_award(conn, "A4", referenced_idv_piid="47QTCB22D0001", description="CLOUD HOSTING")
    add_description(conn, "N1", "Orders under 47QTCB22D0001. Estimated value $1.2 million.")

    add_notice(
        conn,
        "N2",
        title="Logistics data analytics",
        subtier_code="2100",
        office_code="W91QUZ",
        solicitation_number="W91QUZ26R0001",
    )
    add_description(conn, "N2", "Follow-on to the incumbent contract W91QUZ-21-C-0001.")
    add_award(
        conn,
        "W91QUZ21C0001",
        award_type_code="D",
        awarding_sub_agency_code="2100",
        awarding_office_code="W91QUZ",
        ultimate_end=date(2029, 9, 30),  # outside the window: found only by the citation
        description="DATA ANALYTICS",
    )

    add_notice(conn, "OLD", active=False, solicitation_number="47QTCA-25-Q-0099")
    add_award(conn, "WON", solicitation_id="47QTCA25Q0099", ultimate_end=date(2031, 1, 1))
    add_award(conn, "ELSEWHERE", solicitation_id="47QTCA25Q0099", awarding_sub_agency_code="9700")
    add_notice(conn, "AWD", type="Award Notice", active=False, award={"number": "47QTCA-20-F-0001"})
    add_award(conn, "47QTCA20F0001", ultimate_end=date(2024, 1, 1))
    return conn

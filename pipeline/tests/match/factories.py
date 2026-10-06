"""Insert minimal synthetic notices and awards for matcher tests."""

from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

Conn = psycopg.Connection[TupleRow]

NOTICE_DEFAULTS: dict[str, Any] = {
    "title": "IT Help Desk Support Services",
    "type": "Solicitation",
    "posted_at": datetime(2026, 10, 1, tzinfo=UTC),
    "response_deadline": datetime(2026, 12, 1, 17, tzinfo=UTC),
    "naics": "541512",
    "naics_codes": ["541512"],
    "psc": "DA01",
    "set_aside_code": "SBA",
    "subtier_code": "4732",
    "office_code": "47QTCA",
    "active": True,
    "content_hash": "x",
}

AWARD_DEFAULTS: dict[str, Any] = {
    "award_type_code": "C",
    "awarding_agency_code": "047",
    "awarding_sub_agency_code": "4732",
    "awarding_sub_agency_name": "FEDERAL ACQUISITION SERVICE",
    "awarding_office_code": "47QTCA",
    "awarding_office_name": "GSA/FAS IT CENTER",
    "naics": "541512",
    "psc": "DA01",
    "set_aside_code": "SBA",
    "total_value": 1_000_000,
    "ultimate_end": date(2027, 1, 15),
    "description": "HELP DESK SUPPORT TASK ORDER",
    "recipient_uei": "UEI0000000A1",
    "recipient_name": "ACME IT LLC",
}


def _insert(conn: Conn, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    row = [Jsonb(v) if isinstance(v, dict) else v for v in values.values()]
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row)


def add_notice(conn: Conn, notice_id: str, **fields: Any) -> None:
    _insert(conn, "notices", {"notice_id": notice_id, **NOTICE_DEFAULTS, **fields})


def add_award(conn: Conn, award_key: str, **fields: Any) -> None:
    values = {"award_key": award_key, "piid": award_key, **AWARD_DEFAULTS, **fields}
    _insert(conn, "awards", values)


def add_description(conn: Conn, notice_id: str, text: str, version: int = 1) -> None:
    conn.execute(
        "INSERT INTO notice_descriptions (notice_id, version, text) VALUES (%s, %s, %s)",
        (notice_id, version, text),
    )

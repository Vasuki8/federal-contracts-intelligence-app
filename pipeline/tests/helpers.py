"""Helpers shared by tests."""

from pathlib import Path

import psycopg
from psycopg.rows import TupleRow

FIXTURES = Path(__file__).parent / "fixtures"


def count(conn: psycopg.Connection[TupleRow], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])

"""Agencies and offices, built from the organization codes on notices and awards."""

from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

from pipeline.normalize import clean_text


@dataclass(frozen=True)
class OrgPath:
    department_code: str | None = None
    department_name: str | None = None
    subtier_code: str | None = None
    subtier_name: str | None = None
    office_code: str | None = None
    office_name: str | None = None


def parse_parent_path(codes: str | None, names: str | None) -> OrgPath:
    """Split SAM's `fullParentPathCode` ("047.4732.47QTCA") and `fullParentPathName`.

    Codes never contain dots, but names can ("U.S. ..."), so names are only split
    when the piece count matches the codes; otherwise names are left empty.
    """
    code_parts = [clean_text(part) for part in (codes or "").split(".")] if codes else []
    name_parts = (names or "").split(".") if names else []
    if len(name_parts) != len(code_parts):
        name_parts = []
    named = [clean_text(part) for part in name_parts] or [None] * len(code_parts)

    def at(parts: list[str | None], index: int) -> str | None:
        return parts[index] if index < len(parts) else None

    return OrgPath(
        department_code=at(code_parts, 0),
        department_name=at(named, 0),
        subtier_code=at(code_parts, 1),
        subtier_name=at(named, 1),
        office_code=at(code_parts, 2),
        office_name=at(named, 2),
    )


def upsert_org_path(conn: psycopg.Connection[TupleRow], path: OrgPath) -> int | None:
    """Upsert the department, sub-tier and office; return the office id if there is one."""
    if path.department_code:
        _upsert_agency(conn, "department", path.department_code, path.department_name, None)
    if path.subtier_code:
        _upsert_agency(conn, "subtier", path.subtier_code, path.subtier_name, path.department_code)
    if not (path.subtier_code and path.office_code):
        return None
    row = conn.execute(
        """
        INSERT INTO offices (subtier_code, office_code, name) VALUES (%s, %s, %s)
        ON CONFLICT (subtier_code, office_code) DO UPDATE SET
            name = coalesce(excluded.name, offices.name), updated_at = now()
        RETURNING id
        """,
        (path.subtier_code, path.office_code, path.office_name),
    ).fetchone()
    return int(row[0]) if row else None


def _upsert_agency(
    conn: psycopg.Connection[TupleRow],
    level: str,
    code: str,
    name: str | None,
    parent_code: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO agencies (level, code, name, parent_code) VALUES (%s, %s, %s, %s)
        ON CONFLICT (level, code) DO UPDATE SET
            name = coalesce(excluded.name, agencies.name),
            parent_code = coalesce(excluded.parent_code, agencies.parent_code),
            updated_at = now()
        """,
        (level, code, name, parent_code),
    )

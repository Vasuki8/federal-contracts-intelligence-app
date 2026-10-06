"""Upsert notices, keeping one `notice_versions` row per detected change."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.ingest.opportunities.parser import Notice
from pipeline.ingest.orgs import parse_parent_path, upsert_org_path


@dataclass
class UpsertResult:
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.changed


def diff_snapshots(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, list[Any]]:
    """{field: [old, new]} for every field whose value changed."""
    return {
        key: [old.get(key), new.get(key)]
        for key in sorted(set(old) | set(new))
        if old.get(key) != new.get(key)
    }


_COLUMNS = (
    "solicitation_number",
    "title",
    "type",
    "base_type",
    "posted_at",
    "response_deadline",
    "response_deadline_has_time",
    "naics",
    "naics_codes",
    "psc",
    "set_aside_code",
    "set_aside",
    "department_code",
    "subtier_code",
    "office_code",
    "office_id",
    "full_parent_path_code",
    "full_parent_path_name",
    "place_of_performance",
    "pop_state",
    "description_url",
    "attachment_links",
    "contacts",
    "award",
    "ui_link",
    "active",
    "archive_type",
    "archive_date",
    "content_hash",
    "raw_file_id",
)


def _row(conn: psycopg.Connection[TupleRow], notice: Notice, raw_file_id: int | None) -> list[Any]:
    path = parse_parent_path(notice.full_parent_path_code, notice.full_parent_path_name)
    office_id = upsert_org_path(conn, path)
    pop = notice.place_of_performance
    return [
        notice.solicitation_number,
        notice.title,
        notice.type,
        notice.base_type,
        notice.posted_at,
        notice.response_deadline,
        notice.response_deadline_has_time,
        notice.naics,
        list(notice.naics_codes),
        notice.psc,
        notice.set_aside_code,
        notice.set_aside,
        path.department_code,
        path.subtier_code,
        path.office_code,
        office_id,
        notice.full_parent_path_code,
        notice.full_parent_path_name,
        Jsonb(pop) if pop is not None else None,
        notice.pop_state,
        notice.description_url,
        Jsonb(list(notice.attachment_links)),
        Jsonb(list(notice.contacts)),
        Jsonb(notice.award) if notice.award is not None else None,
        notice.ui_link,
        notice.active,
        notice.archive_type,
        notice.archive_date,
        notice.content_hash(),
        raw_file_id,
    ]


def upsert_notices(
    conn: psycopg.Connection[TupleRow], notices: Iterable[Notice], raw_file_id: int | None
) -> UpsertResult:
    """New notice → version 1. Changed content → next version with a diff.
    Same content → only `last_seen_at` moves, so re-runs never add rows."""
    result = UpsertResult()
    for notice in notices:
        current = conn.execute(
            "SELECT content_hash, latest_version FROM notices WHERE notice_id = %s FOR UPDATE",
            (notice.notice_id,),
        ).fetchone()
        new_hash = notice.content_hash()
        if current is not None and current[0] == new_hash:
            conn.execute(
                "UPDATE notices SET last_seen_at = now() WHERE notice_id = %s", (notice.notice_id,)
            )
            result.unchanged += 1
            continue
        values = _row(conn, notice, raw_file_id)
        if current is None:
            _insert(conn, notice, values, raw_file_id)
            result.inserted += 1
        else:
            _new_version(conn, notice, values, int(current[1]) + 1, raw_file_id)
            result.changed += 1
    return result


def _insert(
    conn: psycopg.Connection[TupleRow], notice: Notice, values: list[Any], raw_file_id: int | None
) -> None:
    columns = ", ".join(("notice_id", *_COLUMNS))
    placeholders = ", ".join(["%s"] * (len(_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO notices ({columns}) VALUES ({placeholders})", [notice.notice_id, *values]
    )
    _write_version(conn, notice, 1, None, raw_file_id)


def _new_version(
    conn: psycopg.Connection[TupleRow],
    notice: Notice,
    values: list[Any],
    version: int,
    raw_file_id: int | None,
) -> None:
    previous = conn.execute(
        "SELECT snapshot FROM notice_versions WHERE notice_id = %s ORDER BY version DESC LIMIT 1",
        (notice.notice_id,),
    ).fetchone()
    old_snapshot: Mapping[str, Any] = previous[0] if previous else {}
    assignments = ", ".join(f"{column} = %s" for column in _COLUMNS)
    conn.execute(
        f"UPDATE notices SET {assignments}, latest_version = %s, last_seen_at = now() "
        "WHERE notice_id = %s",
        [*values, version, notice.notice_id],
    )
    _write_version(
        conn, notice, version, diff_snapshots(old_snapshot, notice.snapshot()), raw_file_id
    )


def _write_version(
    conn: psycopg.Connection[TupleRow],
    notice: Notice,
    version: int,
    diff: dict[str, list[Any]] | None,
    raw_file_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO notice_versions (notice_id, version, diff, snapshot, raw_file_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            notice.notice_id,
            version,
            Jsonb(diff) if diff is not None else None,
            Jsonb(notice.snapshot()),
            raw_file_id,
        ),
    )

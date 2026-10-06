"""Upsert notices from more than one source, with one `notice_versions` row per real change.

Notices arrive from the Get Opportunities API (attachments, contacts, every NAICS code)
and from SAM.gov's CSV extracts (description text; every active and archived notice).

- **Merging.** Each source lists the fields it is authoritative for. Other fields only
  fill in values we don't know yet, so the CSV never erases what the API told us.
- **Versions.** A new version is written only when a *tracked* field changes, i.e. what
  an amendment alert would report. Learning a value we didn't know (attachments for a
  notice first seen in the CSV, a first description) is a fill-in, not a change. Any
  other difference updates the row and the latest version's snapshot in place.
- **Descriptions.** Text goes to `notice_descriptions` for the version it belongs to.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.ingest.opportunities.parser import Notice
from pipeline.ingest.orgs import OrgPath, parse_parent_path, upsert_org_path
from pipeline.normalize import normalize_contract_id

Conn = psycopg.Connection[TupleRow]
Snapshot = dict[str, Any]

DESCRIPTION_HASH = "description_sha256"
TRACKED = (
    "title",
    "solicitation_number",
    "type",
    "response_deadline",
    "set_aside_code",
    "naics",
    "psc",
    "active",
    "archive_date",
    "award_number",
    "attachment_links",
    DESCRIPTION_HASH,
)
# Tracked fields a source may simply not know; None means "unknown" only for these.
MAY_BE_UNKNOWN = frozenset({"attachment_links", DESCRIPTION_HASH})


@dataclass(frozen=True)
class IncomingNotice:
    notice: Notice
    description: str | None = None

    def snapshot(self) -> Snapshot:
        snapshot = self.notice.snapshot()
        text = (self.description or "").strip()
        snapshot[DESCRIPTION_HASH] = hashlib.sha256(text.encode()).hexdigest() if text else None
        return snapshot


@dataclass
class UpsertResult:
    inserted: int = 0
    changed: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.changed + self.updated


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def merge(
    stored: Mapping[str, Any], incoming: Mapping[str, Any], authoritative: frozenset[str] | None
) -> Snapshot:
    """`incoming` overwrites the fields it is authoritative for (all when None) and fills in
    the rest where `stored` has nothing. A partial source never blanks a value, a missing
    description never erases one, and a date-only deadline never replaces the same day's
    deadline with a time."""
    merged = dict(stored)
    for key, value in incoming.items():
        if value is None and (authoritative is not None or key == DESCRIPTION_HASH):
            # A partial source (the CSV) can replace a value but never blank it out:
            # an empty cell is not evidence that the API's value went away.
            continue
        if authoritative is None or key in authoritative or merged.get(key) is None:
            merged[key] = value
    if (
        stored.get("response_deadline_has_time")
        and merged.get("response_deadline_has_time") is False
        and _day(stored.get("response_deadline")) == _day(merged.get("response_deadline"))
    ):
        merged["response_deadline"] = stored["response_deadline"]
        merged["response_deadline_has_time"] = True
    return merged


def tracked_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, list[Any]]:
    """{field: [old, new]} for tracked fields that really changed (fill-ins excluded)."""
    a, b = _tracked(old), _tracked(new)
    changes = {}
    for key in TRACKED:
        if a[key] == b[key] or (key in MAY_BE_UNKNOWN and a[key] is None):
            continue
        if key == "response_deadline" and _same_deadline(old, new):
            continue
        changes[key] = [a[key], b[key]]
    return changes


def diff_snapshots(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, list[Any]]:
    """{field: [old, new]} for every field whose value changed."""
    return {
        key: [old.get(key), new.get(key)]
        for key in sorted(set(old) | set(new))
        if old.get(key) != new.get(key)
    }


def _tracked(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    award = snapshot.get("award")
    number = award.get("number") if isinstance(award, Mapping) else None
    links = snapshot.get("attachment_links")
    title = snapshot.get("title")
    return {
        "title": " ".join(title.split()) if isinstance(title, str) else None,
        "solicitation_number": normalize_contract_id(snapshot.get("solicitation_number")),
        "type": snapshot.get("type"),
        "response_deadline": snapshot.get("response_deadline"),
        "set_aside_code": snapshot.get("set_aside_code"),
        "naics": snapshot.get("naics"),
        "psc": snapshot.get("psc"),
        "active": snapshot.get("active"),
        "archive_date": snapshot.get("archive_date"),
        "award_number": normalize_contract_id(number if isinstance(number, str) else None),
        "attachment_links": sorted(links) if links is not None else None,
        DESCRIPTION_HASH: snapshot.get(DESCRIPTION_HASH),
    }


def _day(value: Any) -> str | None:
    return value[:10] if isinstance(value, str) else None


def _same_deadline(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Same day, and one of the two values carries no time: only the precision differs."""
    no_time = old.get("response_deadline_has_time") is False or (
        new.get("response_deadline_has_time") is False
    )
    day = _day(old.get("response_deadline"))
    return no_time and day is not None and day == _day(new.get("response_deadline"))


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


class NoticeWriter:
    """Writes notices for one batch; caches office lookups across the batch."""

    def __init__(self, conn: Conn, raw_file_id: int | None) -> None:
        self._conn = conn
        self._raw_file_id = raw_file_id
        self._offices: dict[OrgPath, int | None] = {}

    def values(self, snapshot: Snapshot) -> list[Any]:
        notice = Notice.model_validate({key: snapshot.get(key) for key in Notice.model_fields})
        path = parse_parent_path(notice.full_parent_path_code, notice.full_parent_path_name)
        if path not in self._offices:
            self._offices[path] = upsert_org_path(self._conn, path)
        pop, award = notice.place_of_performance, notice.award
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
            self._offices[path],
            notice.full_parent_path_code,
            notice.full_parent_path_name,
            Jsonb(pop) if pop is not None else None,
            notice.pop_state,
            notice.description_url,
            Jsonb(list(notice.attachment_links or ())),
            Jsonb(list(notice.contacts or ())),
            Jsonb(award) if award is not None else None,
            notice.ui_link,
            notice.active,
            notice.archive_type,
            notice.archive_date,
            snapshot_hash(snapshot),
            self._raw_file_id,
        ]

    def insert(self, notice_id: str, snapshot: Snapshot) -> None:
        columns = ", ".join(("notice_id", *_COLUMNS))
        placeholders = ", ".join(["%s"] * (len(_COLUMNS) + 1))
        self._conn.execute(
            f"INSERT INTO notices ({columns}) VALUES ({placeholders})",
            [notice_id, *self.values(snapshot)],
        )
        self.write_version(notice_id, 1, None, snapshot)

    def update(self, notice_id: str, snapshot: Snapshot, version: int) -> None:
        assignments = ", ".join(f"{column} = %s" for column in _COLUMNS)
        self._conn.execute(
            f"UPDATE notices SET {assignments}, latest_version = %s, last_seen_at = now() "
            "WHERE notice_id = %s",
            [*self.values(snapshot), version, notice_id],
        )

    def write_version(
        self, notice_id: str, version: int, diff: dict[str, list[Any]] | None, snapshot: Snapshot
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO notice_versions (notice_id, version, diff, snapshot, raw_file_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (notice_id, version) DO UPDATE SET snapshot = excluded.snapshot
            """,
            (
                notice_id,
                version,
                Jsonb(diff) if diff is not None else None,
                Jsonb(snapshot),
                self._raw_file_id,
            ),
        )

    def write_description(self, notice_id: str, version: int, text: str) -> None:
        self._conn.execute(
            """
            INSERT INTO notice_descriptions (notice_id, version, text, raw_file_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (notice_id, version) DO UPDATE SET
                text = excluded.text, raw_file_id = excluded.raw_file_id, fetched_at = now()
            WHERE notice_descriptions.text IS DISTINCT FROM excluded.text
            """,
            (notice_id, version, text, self._raw_file_id),
        )


def upsert_notices(
    conn: Conn,
    notices: Iterable[Notice | IncomingNotice],
    raw_file_id: int | None,
    authoritative: frozenset[str] | None = None,
) -> UpsertResult:
    """Merge each notice into what we have (see the module docstring).
    Re-running with the same input writes nothing but `last_seen_at`."""
    items = [n if isinstance(n, IncomingNotice) else IncomingNotice(n) for n in notices]
    result = UpsertResult()
    if not items:
        return result
    stored = _stored_snapshots(conn, [item.notice.notice_id for item in items])
    writer = NoticeWriter(conn, raw_file_id)
    unchanged: list[str] = []
    for item in items:
        notice_id = item.notice.notice_id
        incoming = item.snapshot()
        current = stored.get(notice_id)
        text = (item.description or "").strip()
        if current is None:
            writer.insert(notice_id, incoming)
            if text:
                writer.write_description(notice_id, 1, text)
            stored[notice_id] = (snapshot_hash(incoming), 1, incoming)
            result.inserted += 1
            continue
        old_hash, version, old = current
        merged = merge(old, incoming, authoritative)
        new_hash = snapshot_hash(merged)
        if new_hash == old_hash:
            unchanged.append(notice_id)
            result.unchanged += 1
            continue
        changes = tracked_changes(old, merged)
        if changes:
            version += 1
            result.changed += 1
        else:
            result.updated += 1
        writer.update(notice_id, merged, version)
        writer.write_version(notice_id, version, changes or None, merged)
        if text and merged.get(DESCRIPTION_HASH) != old.get(DESCRIPTION_HASH):
            writer.write_description(notice_id, version, text)
        stored[notice_id] = (new_hash, version, merged)
    if unchanged:
        conn.execute(
            "UPDATE notices SET last_seen_at = now() WHERE notice_id = ANY(%s)", (unchanged,)
        )
    return result


def _stored_snapshots(conn: Conn, notice_ids: list[str]) -> dict[str, tuple[str, int, Snapshot]]:
    """notice_id → (content_hash, latest_version, latest snapshot), locked for this batch."""
    rows = conn.execute(
        """
        SELECT n.notice_id, n.content_hash, n.latest_version, v.snapshot
        FROM notices n
        JOIN notice_versions v ON v.notice_id = n.notice_id AND v.version = n.latest_version
        WHERE n.notice_id = ANY(%s)
        ORDER BY n.notice_id
        FOR UPDATE OF n
        """,
        (sorted(set(notice_ids)),),
    ).fetchall()
    return {row[0]: (row[1], int(row[2]), dict(row[3])) for row in rows}

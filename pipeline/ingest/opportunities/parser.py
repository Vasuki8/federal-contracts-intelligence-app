"""Parse Get Opportunities API pages into Notice records.

Field names come from the official docs. Where the docs' field table and example
response disagree, both documented spellings are accepted (see docs/data-sources.md);
any key not documented at all is reported so a live response can settle it.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from pipeline.normalize import clean_text, parse_date, parse_datetime

# Every top-level record key the docs mention, including deprecated ones and both
# spellings where the field table and the example response differ.
DOCUMENTED_FIELDS = frozenset(
    {
        "noticeId",
        "title",
        "solicitationNumber",
        "fullParentPathName",
        "fullParentPathCode",
        "department",
        "subTier",
        "subtier",
        "office",
        "postedDate",
        "type",
        "baseType",
        "archiveType",
        "archiveDate",
        "typeOfSetAsideDescription",
        "typeOfSetAside",
        "setAside",
        "setAsideCode",
        "responseDeadLine",
        "reponseDeadLine",
        "naicsCode",
        "classificationCode",
        "active",
        "award",
        "pointOfContact",
        "pointofContact",
        "description",
        "organizationType",
        "officeAddress",
        "placeOfPerformance",
        "additionalInfoLink",
        "uiLink",
        "links",
        "resourceLinks",
    }
)

# Keys seen in live responses that the docs don't mention (first live sample, 2026-10-06).
OBSERVED_FIELDS = frozenset({"naicsCodes"})
KNOWN_FIELDS = DOCUMENTED_FIELDS | OBSERVED_FIELDS


class Notice(BaseModel):
    """One notice as we store it. Field order is stable so hashes are stable."""

    model_config = ConfigDict(frozen=True)

    notice_id: str
    solicitation_number: str | None
    title: str | None
    type: str | None
    base_type: str | None
    posted_at: datetime | None
    response_deadline: datetime | None
    # False when SAM gave only a date: the deadline is then stored at 00:00 UTC on that
    # date and must be shown as a date, never with an invented time.
    response_deadline_has_time: bool | None
    naics: str | None
    naics_codes: tuple[str, ...]
    psc: str | None
    set_aside_code: str | None
    set_aside: str | None
    full_parent_path_code: str | None
    full_parent_path_name: str | None
    place_of_performance: dict[str, Any] | None
    pop_state: str | None
    description_url: str | None
    attachment_links: tuple[str, ...]
    contacts: tuple[dict[str, Any], ...]
    award: dict[str, Any] | None
    ui_link: str | None
    active: bool | None
    archive_type: str | None
    archive_date: date | None

    def in_naics(self, codes: frozenset[str]) -> bool:
        """True when the primary or any listed NAICS code is in `codes`."""
        return self.naics in codes or any(code in codes for code in self.naics_codes)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dict of every field; stored per version."""
        return self.model_dump(mode="json")

    def content_hash(self) -> str:
        canonical = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class ParsedPage:
    total_records: int
    notices: list[Notice]
    skipped: int = 0
    unknown_fields: set[str] = field(default_factory=set)


def parse_page(payload: Mapping[str, Any]) -> ParsedPage:
    records = payload.get("opportunitiesData") or []
    page = ParsedPage(total_records=int(payload.get("totalRecords") or 0), notices=[])
    for record in records:
        if not isinstance(record, Mapping):
            page.skipped += 1
            continue
        page.unknown_fields |= set(record) - KNOWN_FIELDS
        notice = parse_notice(record)
        if notice is None:
            page.skipped += 1
        else:
            page.notices.append(notice)
    return page


def parse_notice(record: Mapping[str, Any]) -> Notice | None:
    """A Notice, or None when the record has no noticeId."""
    notice_id = clean_text(record.get("noticeId"))
    if notice_id is None:
        return None
    pop = _mapping(record.get("placeOfPerformance")) or None
    naics = clean_text(record.get("naicsCode"))
    deadline = _first(record, "responseDeadLine", "reponseDeadLine")
    return Notice(
        notice_id=notice_id,
        solicitation_number=clean_text(record.get("solicitationNumber")),
        title=clean_text(record.get("title")),
        type=clean_text(record.get("type")),
        base_type=clean_text(record.get("baseType")),
        posted_at=parse_datetime(record.get("postedDate")),
        response_deadline=parse_datetime(deadline),
        response_deadline_has_time=_has_time(deadline),
        naics=naics,
        naics_codes=tuple(_strings(record.get("naicsCodes")) or ([naics] if naics else [])),
        psc=clean_text(record.get("classificationCode")),
        set_aside_code=clean_text(_first(record, "typeOfSetAside", "setAsideCode")),
        set_aside=clean_text(_first(record, "typeOfSetAsideDescription", "setAside")),
        full_parent_path_code=clean_text(record.get("fullParentPathCode")),
        full_parent_path_name=clean_text(record.get("fullParentPathName"))
        or _legacy_org_name(record),
        place_of_performance=pop,
        pop_state=clean_text(_mapping(pop.get("state")).get("code")) if pop else None,
        description_url=clean_text(record.get("description")),
        attachment_links=tuple(_strings(record.get("resourceLinks"))),
        contacts=tuple(_contacts(_first(record, "pointOfContact", "pointofContact"))),
        award=_mapping(record.get("award")) or None,
        ui_link=clean_text(record.get("uiLink")),
        active=_yes_no(record.get("active")),
        archive_type=clean_text(record.get("archiveType")),
        archive_date=parse_date(record.get("archiveDate")),
    )


def _first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [text for item in value if (text := clean_text(item)) is not None]


def _contacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _has_time(value: Any) -> bool | None:
    """Whether a date value carries a time part ("2026-10-13" → False)."""
    if parse_datetime(value) is None:
        return None
    text = clean_text(value) or ""
    return len(text) > len("YYYY-MM-DD")


def _yes_no(value: Any) -> bool | None:
    text = clean_text(value)
    if text is None:
        return None
    return text.lower() in {"yes", "true", "y"}


def _legacy_org_name(record: Mapping[str, Any]) -> str | None:
    """Older responses carry department / subTier / office names instead of the full path."""
    parts = [
        clean_text(record.get("department")),
        clean_text(record.get("subTier") or record.get("subtier")),
        clean_text(record.get("office")),
    ]
    named = [part for part in parts if part]
    return ".".join(named) if named else None

"""SAM.gov Contract Opportunities CSV extracts: public files, no API key, no request limit.

- Daily (`ContractOpportunitiesFullCSV.csv`, ~200 MB): every active notice, with the full
  description text (SAM cuts it at about 32,000 characters).
- Archived (`FY<year>_archived_opportunities.csv`, ~1 GB each, refreshed weekly): notices
  archived in that fiscal year.

Both are quoted CSV in Windows-1252 with the same 47 columns. The header is checked on
every load, so a changed layout stops the job instead of loading wrong values."""

import csv
import gzip
import hashlib
import io
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.ingest.opportunities.parser import Notice, has_time, yes_no
from pipeline.ingest.opportunities.store import IncomingNotice
from pipeline.normalize import clean_text, parse_date, parse_datetime

SOURCE = "sam_extract"
BASE_URL = "https://s3.amazonaws.com/falextracts/Contract%20Opportunities"
DAILY_URL = f"{BASE_URL}/datagov/ContractOpportunitiesFullCSV.csv"
ENCODING = "cp1252"


def archive_url(fiscal_year: int) -> str:
    return f"{BASE_URL}/Archived%20Data/FY{fiscal_year}_archived_opportunities.csv"


# The header as published (checked 2026-10-06 on the daily and FY2026 archive files).
COLUMNS = (
    "NoticeId",
    "Title",
    "Sol#",
    "Department/Ind.Agency",
    "CGAC",
    "Sub-Tier",
    "FPDS Code",
    "Office",
    "AAC Code",
    "PostedDate",
    "Type",
    "BaseType",
    "ArchiveType",
    "ArchiveDate",
    "SetASideCode",
    "SetASide",
    "ResponseDeadLine",
    "NaicsCode",
    "ClassificationCode",
    "PopStreetAddress",
    "PopCity",
    "PopState",
    "PopZip",
    "PopCountry",
    "Active",
    "AwardNumber",
    "AwardDate",
    "Award$",
    "Awardee",
    "PrimaryContactTitle",
    "PrimaryContactFullname",
    "PrimaryContactEmail",
    "PrimaryContactPhone",
    "PrimaryContactFax",
    "SecondaryContactTitle",
    "SecondaryContactFullname",
    "SecondaryContactEmail",
    "SecondaryContactPhone",
    "SecondaryContactFax",
    "OrganizationType",
    "State",
    "City",
    "ZipCode",
    "CountryCode",
    "AdditionalInfoLink",
    "Link",
    "Description",
)

# For a notice we already have, the CSV may change these; every other field it only fills
# in when unknown (the API's attachments, contacts and NAICS lists are richer).
AUTHORITATIVE = frozenset(
    {
        "title",
        "solicitation_number",
        "type",
        "base_type",
        "response_deadline",
        "response_deadline_has_time",
        "naics",
        "psc",
        "set_aside_code",
        "set_aside",
        "active",
        "archive_type",
        "archive_date",
        "description_sha256",
    }
)


class ExtractFormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class Header:
    columns: tuple[str, ...]
    unknown: tuple[str, ...]


def check_header(header: Sequence[str]) -> Header:
    missing = [column for column in COLUMNS if column not in header]
    if missing:
        raise ExtractFormatError(
            f"The SAM.gov extract is missing expected columns {missing}; "
            "update pipeline/ingest/opportunities/extract.py and docs/data-sources.md."
        )
    return Header(tuple(header), tuple(column for column in header if column not in COLUMNS))


def read_rows(path: Path) -> tuple[Header, Iterator[list[str]]]:
    """The checked header and an iterator over raw rows (lists of strings, as published)."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    handle = path.open(encoding=ENCODING, errors="replace", newline="")
    reader = csv.reader(handle)
    try:
        header = check_header(next(reader))
    except StopIteration as exc:
        handle.close()
        raise ExtractFormatError("The SAM.gov extract is empty.") from exc

    def rows() -> Iterator[list[str]]:
        with handle:
            yield from reader

    return header, rows()


def as_record(header: Header, row: Sequence[str]) -> dict[str, str]:
    return dict(zip(header.columns, row, strict=False))


def parse_row(record: Mapping[str, str]) -> IncomingNotice | None:
    """A notice (fields the CSV doesn't have stay unknown) plus its description text."""
    notice_id = clean_text(record.get("NoticeId"))
    if notice_id is None:
        return None
    naics = clean_text(record.get("NaicsCode"))
    deadline = clean_text(record.get("ResponseDeadLine"))
    notice = Notice(
        notice_id=notice_id,
        solicitation_number=clean_text(record.get("Sol#")),
        title=clean_text(record.get("Title")),
        type=clean_text(record.get("Type")),
        base_type=clean_text(record.get("BaseType")),
        posted_at=parse_datetime(record.get("PostedDate")),
        response_deadline=parse_datetime(deadline),
        response_deadline_has_time=has_time(deadline),
        naics=naics,
        naics_codes=(naics,) if naics else (),
        psc=clean_text(record.get("ClassificationCode")),
        set_aside_code=clean_text(record.get("SetASideCode")),
        set_aside=clean_text(record.get("SetASide")),
        full_parent_path_code=_joined(record, "CGAC", "FPDS Code", "AAC Code"),
        full_parent_path_name=_joined(record, "Department/Ind.Agency", "Sub-Tier", "Office"),
        place_of_performance=_place(record),
        pop_state=clean_text(record.get("PopState")),
        description_url=None,
        attachment_links=None,
        contacts=_contacts(record),
        award=_award(record),
        ui_link=clean_text(record.get("Link")),
        active=yes_no(record.get("Active")),
        archive_type=clean_text(record.get("ArchiveType")),
        archive_date=parse_date(record.get("ArchiveDate")),
    )
    return IncomingNotice(notice, clean_text(record.get("Description")))


def in_scope(record: Mapping[str, str], vertical: frozenset[str], since: date | None) -> bool:
    if clean_text(record.get("NaicsCode")) not in vertical:
        return False
    if since is None:
        return True
    posted = parse_datetime(record.get("PostedDate"))
    return posted is not None and posted.date() >= since


def vertical_csv(header: Header, rows: Sequence[Sequence[str]]) -> bytes:
    """The kept rows, unchanged, as a UTF-8 CSV for the raw archive."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header.columns)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def gzip_bytes(content: bytes) -> bytes:
    return gzip.compress(content, mtime=0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _joined(record: Mapping[str, str], *columns: str) -> str | None:
    parts = [part for column in columns if (part := clean_text(record.get(column)))]
    return ".".join(parts) if parts else None


def _place(record: Mapping[str, str]) -> dict[str, Any] | None:
    place: dict[str, Any] = {}
    if street := clean_text(record.get("PopStreetAddress")):
        place["streetAddress"] = street
    if city := clean_text(record.get("PopCity")):
        place["city"] = {"name": city}
    if state := clean_text(record.get("PopState")):
        place["state"] = {"code": state}
    if zip_code := clean_text(record.get("PopZip")):
        place["zip"] = zip_code
    if country := clean_text(record.get("PopCountry")):
        place["country"] = {"code": country}
    return place or None


def _contacts(record: Mapping[str, str]) -> tuple[dict[str, Any], ...]:
    """Same keys as the API's `pointOfContact` items."""
    contacts = []
    for kind, prefix in (("primary", "PrimaryContact"), ("secondary", "SecondaryContact")):
        values = {
            "title": clean_text(record.get(f"{prefix}Title")),
            "fullName": clean_text(record.get(f"{prefix}Fullname")),
            "email": clean_text(record.get(f"{prefix}Email")),
            "phone": clean_text(record.get(f"{prefix}Phone")),
            "fax": clean_text(record.get(f"{prefix}Fax")),
        }
        if any(values.values()):
            contacts.append({"type": kind, **values})
    return tuple(contacts)


def _award(record: Mapping[str, str]) -> dict[str, Any] | None:
    """Same keys as the API's `award` object. `Awardee` holds the name and address as one
    string, so it is kept whole."""
    award: dict[str, Any] = {}
    if number := clean_text(record.get("AwardNumber")):
        award["number"] = number
    if day := clean_text(record.get("AwardDate")):
        award["date"] = day
    if amount := clean_text(record.get("Award$")):
        award["amount"] = amount
    if awardee := clean_text(record.get("Awardee")):
        award["awardee"] = {"name": awardee}
    return award or None

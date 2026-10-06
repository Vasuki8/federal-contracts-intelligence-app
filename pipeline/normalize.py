"""Normalization helpers shared by parsers and the matcher."""

import re
from datetime import UTC, date, datetime

_ID_NOISE = re.compile(r"[\s\-]+")


def normalize_contract_id(value: str | None) -> str | None:
    """Uppercase and drop spaces and dashes, for solicitation numbers and PIIDs.

    Must stay in sync with the generated `*_norm` columns in migration 0002.
    """
    if value is None:
        return None
    normalized = _ID_NOISE.sub("", value).upper()
    return normalized or None


def clean_text(value: object) -> str | None:
    """Strip a string value; empty strings and the literal "null" become None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def parse_datetime(value: object) -> datetime | None:
    """Parse ISO-8601 dates/datetimes ("2024-05-04", "2024-05-04 13:00:00",
    "2024-05-04T13:00:00-04:00"). Values without a timezone are taken as UTC."""
    text = clean_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_date(value: object) -> date | None:
    """Parse "YYYY-MM-DD" (a trailing time part is ignored)."""
    text = clean_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None

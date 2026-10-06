"""Normalization, org path parsing and verticals config."""

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow
from pydantic import ValidationError

from pipeline.ingest.orgs import parse_parent_path
from pipeline.normalize import clean_text, normalize_contract_id, parse_date, parse_datetime
from pipeline.verticals import load_verticals, vertical_naics

IDS = [" 47pf-0018 R0023 ", "W912DY-24-R-0001", "abc\t123", "--", "", None]


@pytest.mark.parametrize("value", IDS)
def test_contract_id_normalization(value: str | None) -> None:
    result = normalize_contract_id(value)
    if value is None or not value.strip(" -"):
        assert result is None
        return
    assert result is not None
    assert result == result.upper()
    assert " " not in result and "-" not in result


@pytest.mark.db
def test_python_and_sql_normalization_agree(conn: psycopg.Connection[TupleRow]) -> None:
    for i, value in enumerate(IDS):
        conn.execute(
            "INSERT INTO notices (notice_id, solicitation_number, content_hash)"
            " VALUES (%s, %s, '')",
            (f"n{i}", value),
        )
        row = conn.execute(
            "SELECT solicitation_number_norm FROM notices WHERE notice_id = %s", (f"n{i}",)
        ).fetchone()
        assert row is not None
        assert row[0] == normalize_contract_id(value), value


def test_datetime_parsing() -> None:
    assert parse_datetime("2018-05-04") == datetime(2018, 5, 4, tzinfo=UTC)
    assert parse_datetime("2018-05-04 13:45:00") == datetime(2018, 5, 4, 13, 45, tzinfo=UTC)
    eastern = timezone(timedelta(hours=-4))
    assert parse_datetime("2024-05-15T14:00:00-04:00") == datetime(2024, 5, 15, 14, tzinfo=eastern)
    assert parse_datetime("null") is None
    assert parse_datetime("not a date") is None
    assert parse_date("2024-09-30 00:00:00") == date(2024, 9, 30)
    assert clean_text("  x ") == "x"


def test_parent_path_splits_codes_and_matching_names() -> None:
    path = parse_parent_path(
        "047.4732.47QTCA",
        "GENERAL SERVICES ADMINISTRATION.FEDERAL ACQUISITION SERVICE.GSA/FAS CENTER FOR IT",
    )
    assert (path.department_code, path.subtier_code, path.office_code) == ("047", "4732", "47QTCA")
    assert path.office_name == "GSA/FAS CENTER FOR IT"


def test_parent_path_drops_names_when_a_name_contains_a_dot() -> None:
    path = parse_parent_path("070.7008.70Z023", "HOMELAND SECURITY.U.S. COAST GUARD.SFLC")
    assert path.subtier_code == "7008"
    assert path.subtier_name is None
    assert path.office_code == "70Z023"


def test_parent_path_with_two_levels_or_nothing() -> None:
    assert parse_parent_path("097.9700", None).office_code is None
    assert parse_parent_path(None, None).department_code is None


def test_default_verticals_file_has_the_eleven_codes() -> None:
    codes = vertical_naics()
    assert len(codes) == 11
    assert {"541511", "541512", "518210", "541330"} <= codes


def test_verticals_reject_bad_or_duplicate_codes(tmp_path: Path) -> None:
    bad = tmp_path / "v.yaml"
    bad.write_text("verticals:\n  - {key: x, name: X, naics: ['5415']}\n")
    with pytest.raises(ValidationError):
        load_verticals(bad)
    bad.write_text("verticals:\n  - {key: x, name: X, naics: ['541511', '541511']}\n")
    with pytest.raises(ValidationError):
        load_verticals(bad)

import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from pipeline.ingest.opportunities.parser import parse_notice, parse_page
from pipeline.tests.helpers import FIXTURES

SAM = FIXTURES / "sam_opportunities"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAM / name).read_text())
    return data


def test_docs_example_1_award_notice() -> None:
    page = parse_page(load("docs_example_1.json"))
    assert page.total_records == 34
    assert page.unknown_fields == set()
    notice = page.notices[0]
    assert notice.notice_id == "5b345bbb7127b91a3ad577b203fc6f68"
    assert notice.solicitation_number == "47PF0018R0023"  # docs value has stray spaces
    assert notice.posted_at == datetime(2018, 5, 4, tzinfo=UTC)
    assert notice.type == "Award Notice"
    assert notice.base_type == "Combined Synopsis/Solicitation"
    assert notice.naics == "236220"
    assert notice.psc == "Z"
    assert notice.active is True
    assert notice.pop_state == "WI"
    assert notice.award is not None
    assert notice.award["awardee"]["ueiSAM"] == "025114695AST"
    assert notice.contacts[0]["fullName"] == "Jesse L. Jones"
    # Deprecated department/subTier/office names are kept when no full path is given.
    assert (
        notice.full_parent_path_name
        == "GENERAL SERVICES ADMINISTRATION.PUBLIC BUILDINGS SERVICE.PBS R5"
    )
    assert notice.full_parent_path_code is None


def test_docs_example_2_v2_fields_and_null_strings() -> None:
    notice = parse_page(load("docs_example_2.json")).notices[0]
    assert notice.full_parent_path_code == "047.4732.47QTCA"
    assert notice.description_url is None  # the docs show the string "null"
    assert notice.ui_link is None
    assert notice.archive_date == date(2021, 1, 2)
    assert notice.place_of_performance is None
    assert notice.attachment_links == ()


def test_field_table_spellings_are_accepted() -> None:
    # The docs' field table uses these names; the example response uses others.
    notice = parse_notice(
        {
            "noticeId": "abc",
            "reponseDeadLine": "2026-11-02T14:00:00-05:00",
            "setAsideCode": "8A",
            "setAside": "8(a) Set-Aside (FAR 19.8)",
            "pointofContact": [{"fullName": "A"}],
        }
    )
    assert notice is not None
    assert notice.response_deadline == datetime(
        2026, 11, 2, 14, tzinfo=timezone(timedelta(hours=-5))
    )
    assert notice.set_aside_code == "8A"
    assert notice.set_aside == "8(a) Set-Aside (FAR 19.8)"
    assert notice.contacts == ({"fullName": "A"},)


def test_unknown_fields_are_reported_and_records_without_id_skipped() -> None:
    page = parse_page(
        {"totalRecords": 2, "opportunitiesData": [{"noticeId": "a", "brandNew": 1}, {"title": "x"}]}
    )
    assert [n.notice_id for n in page.notices] == ["a"]
    assert page.skipped == 1
    assert page.unknown_fields == {"brandNew", "title"} - {"title"}


def test_content_hash_is_stable_and_tracks_changes() -> None:
    v1 = parse_page(load("amendment_v1.json")).notices[0]
    v1_again = parse_page(load("amendment_v1.json")).notices[0]
    v2 = parse_page(load("amendment_v2.json")).notices[0]
    assert v1.content_hash() == v1_again.content_hash()
    assert v1.content_hash() != v2.content_hash()


def test_empty_page() -> None:
    page = parse_page({"totalRecords": 0})
    assert page.notices == []

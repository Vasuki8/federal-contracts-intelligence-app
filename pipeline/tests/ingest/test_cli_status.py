"""`app ingest` argument handling, `app status`, and the sample-fixture helpers."""

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow
from typer.testing import CliRunner

from pipeline import cli
from pipeline.ingest import cli as ingest_cli
from pipeline.ingest.awards.job import AwardsJob
from pipeline.ingest.budget import BudgetExhausted
from pipeline.ingest.context import ingest_run
from pipeline.ingest.samples import header_report, offset_semantics
from pipeline.settings import Settings
from pipeline.status import build_report
from pipeline.tests.helpers import FIXTURES

runner = CliRunner()


@pytest.fixture
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for module in (cli, ingest_cli):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(_env_file=None))
    return monkeypatch


def test_sam_jobs_stop_and_ask_for_a_missing_key(no_dotenv: pytest.MonkeyPatch) -> None:
    no_dotenv.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    result = runner.invoke(cli.app, ["ingest", "opportunities", "--backfill"])
    assert result.exit_code == 2
    assert "SAM_API_KEY is not set. Add it to .env" in result.output


def test_ingest_without_database_url_explains_fix(no_dotenv: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["ingest", "awards-delta"])
    assert result.exit_code == 2
    assert "DATABASE_URL is not set" in result.output


def test_dates_must_be_iso(no_dotenv: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["ingest", "opportunities", "--since", "10/01/2025"])
    assert result.exit_code == 2


@pytest.mark.db
def test_failed_job_exits_nonzero_and_status_shows_the_error(
    db_url: str, no_dotenv: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    no_dotenv.setenv("DATABASE_URL", db_url)
    no_dotenv.setenv("RAW_ARCHIVE_DIR", str(tmp_path))

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("upstream exploded")

    no_dotenv.setattr(AwardsJob, "delta", explode)
    result = runner.invoke(cli.app, ["ingest", "awards-delta"])
    assert result.exit_code == 1
    assert "upstream exploded" in result.output

    status = runner.invoke(cli.app, ["status"])
    assert status.exit_code == 0
    assert "usaspending_awards delta: failed" in status.output
    assert "RuntimeError: upstream exploded" in status.output


@pytest.mark.db
def test_status_report_sections(
    db_url: str, conn: psycopg.Connection[TupleRow], tmp_path: Path
) -> None:
    with ingest_run(db_url, tmp_path, "sam_opportunities", "backfill", {}, lambda _: None):
        raise BudgetExhausted("sam_opportunities", 10)
    text = "\n".join(build_report(conn, 10, datetime.now(UTC)))
    assert "sam_opportunities backfill: partial" in text
    assert "note: Daily request budget" in text
    assert "SAM.gov requests today (UTC): 0 of 10" in text
    assert "notices" in text
    storage = text.split("Storage (MB)\n", 1)[1].splitlines()
    assert storage[0].split()[0] == "database"
    assert storage[-1].split()[:2] == ["raw", "archive"]
    assert len(storage) == 7  # database, the 5 largest tables, raw archive
    limited = "\n".join(build_report(conn, 10, datetime.now(UTC), size_limit_mb=1))
    assert "database is at" in limited and "WARNING: the database is close" in limited


def test_offset_semantics_detection() -> None:
    a, b, c, d = ({"noticeId": x} for x in "abcd")
    assert offset_semantics([a, b, c], [b, c, d]).startswith("record offset")
    assert offset_semantics([a, b], [c, d]).startswith("page index")
    assert offset_semantics([a], [b]).startswith("unknown")


def test_header_report() -> None:
    assert header_report(["x"], ["x"])[0].startswith("Columns the loader uses")
    columns = (FIXTURES / "usaspending" / "award_d1_columns.txt").read_text().split()
    assert header_report(columns, columns) == ["Live award header matches the documented layout."]
    assert "brand_new" in header_report([*columns, "brand_new"], columns)[0]

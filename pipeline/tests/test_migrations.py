import pytest
from alembic import command
from typer.testing import CliRunner

from pipeline import cli
from pipeline.migrate import alembic_config, current_revision, head_revision
from pipeline.settings import Settings

pytestmark = pytest.mark.db


def test_head_is_a_single_revision() -> None:
    assert head_revision() is not None


def test_upgrade_downgrade_upgrade_round_trip(test_database_url: str) -> None:
    config = alembic_config(test_database_url)

    command.downgrade(config, "base")
    assert current_revision(test_database_url) is None

    command.upgrade(config, "head")
    assert current_revision(test_database_url) == head_revision()

    command.downgrade(config, "base")
    assert current_revision(test_database_url) is None

    command.upgrade(config, "head")
    assert current_revision(test_database_url) == head_revision()


def test_doctor_passes_on_migrated_database(
    test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(alembic_config(test_database_url), "head")
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "connection  ok (PostgreSQL 16" in result.output
    assert f"migrations  up to date ({head_revision()})" in result.output

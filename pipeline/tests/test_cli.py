import pytest
from typer.testing import CliRunner

from pipeline import __version__, cli
from pipeline.settings import Settings

runner = CliRunner()


@pytest.fixture
def env_without_dotenv(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear settings env vars and stop the CLI from reading the real .env file."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    return monkeypatch


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output
    assert "doctor" in result.output


def test_version_prints_package_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_doctor_reports_missing_without_leaking_values(
    env_without_dotenv: pytest.MonkeyPatch,
) -> None:
    env_without_dotenv.setenv("SAM_API_KEY", "super-secret-value")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "super-secret-value" not in result.output
    assert any(line.split() == ["SAM_API_KEY", "set"] for line in result.output.splitlines())
    assert "DATABASE_URL is not set" in result.output


def test_doctor_reports_unreachable_database(env_without_dotenv: pytest.MonkeyPatch) -> None:
    # Port 1 on localhost is never a Postgres server.
    env_without_dotenv.setenv("DATABASE_URL", "postgresql://u:secret-pw@127.0.0.1:1/nope")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "connection  failed" in result.output
    assert "docker compose up -d db" in result.output
    assert "secret-pw" not in result.output

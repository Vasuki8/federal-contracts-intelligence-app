import pytest

from pipeline.db import sqlalchemy_url
from pipeline.settings import MissingSettingError, Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


def test_reads_values_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/fci")
    clean_env.setenv("ANTHROPIC_MODEL", "model-from-env")
    settings = Settings(_env_file=None)
    assert settings.require_database_url() == "postgresql://u:p@localhost:5432/fci"
    assert settings.anthropic_model == "model-from-env"


def test_empty_values_count_as_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SAM_API_KEY", "")
    settings = Settings(_env_file=None)
    assert settings.sam_api_key is None
    assert settings.env_status()["SAM_API_KEY"] is False


def test_missing_database_url_explains_fix(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(MissingSettingError, match=r"DATABASE_URL is not set\. Add it to \.env"):
        settings.require_database_url()


def test_secrets_never_appear_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SAM_API_KEY", "super-secret-value")
    clean_env.setenv("DATABASE_URL", "postgresql://u:db-password@localhost/fci")
    settings = Settings(_env_file=None)
    assert "super-secret-value" not in repr(settings)
    assert "db-password" not in repr(settings)


def test_env_status_lists_every_setting(clean_env: pytest.MonkeyPatch) -> None:
    status = Settings(_env_file=None).env_status()
    assert set(status) == {
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "SAM_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ADMIN_PASSWORD",
        "DATABASE_SIZE_LIMIT_MB",
        "RESEND_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "AUTH_SECRET",
        "RAW_ARCHIVE_DIR",
        "SAM_DAILY_REQUEST_LIMIT",
    }


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_sqlalchemy_url_uses_psycopg_driver(given: str, expected: str) -> None:
    assert sqlalchemy_url(given) == expected


def test_sqlalchemy_url_rejects_other_databases() -> None:
    with pytest.raises(ValueError, match="postgresql://"):
        sqlalchemy_url("mysql://u:p@h/db")

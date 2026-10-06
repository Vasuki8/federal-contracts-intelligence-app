"""Typed settings loaded from environment variables and the repo-root `.env` file."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class MissingSettingError(RuntimeError):
    """Raised when a job needs a setting that is not configured."""

    def __init__(self, env_var: str) -> None:
        super().__init__(f"{env_var} is not set. Add it to .env (see .env.example) and try again.")
        self.env_var = env_var


class Settings(BaseSettings):
    """Every setting the pipeline reads. Secrets are SecretStr so they never print."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None
    sam_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    resend_api_key: SecretStr | None = None
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    auth_secret: SecretStr | None = None
    raw_archive_dir: Path = Path("data/raw")

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise MissingSettingError("DATABASE_URL")
        return self.database_url.get_secret_value()

    def env_status(self) -> dict[str, bool]:
        """Map each env var name to whether it is set. Never includes values."""
        return {name.upper(): getattr(self, name) is not None for name in type(self).model_fields}


def get_settings() -> Settings:
    return Settings()

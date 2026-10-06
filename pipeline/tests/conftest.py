"""Shared fixtures. Tests marked `db` use TEST_DATABASE_URL and fail if it is unreachable."""

from urllib.parse import urlparse

import psycopg
import pytest

from pipeline.settings import Settings


@pytest.fixture(scope="session")
def test_database_url() -> str:
    settings = Settings()
    if settings.test_database_url is None:
        pytest.fail("TEST_DATABASE_URL is not set. Copy .env.example to .env.")
    url = settings.test_database_url.get_secret_value()
    database_name = urlparse(url).path.lstrip("/")
    if not database_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run: test database name must end in '_test' (got {database_name!r})."
        )
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Cannot reach the test database. Run: docker compose up -d db\n{exc}")
    return url

"""Shared fixtures. Tests marked `db` use TEST_DATABASE_URL and fail if it is unreachable."""

from collections.abc import Iterator
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from psycopg.rows import TupleRow

from pipeline.migrate import alembic_config
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


@pytest.fixture
def db_url(test_database_url: str) -> str:
    """Test DB migrated to head, with every data table emptied."""
    command.upgrade(alembic_config(test_database_url), "head")
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT quote_ident(table_name) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name <> 'alembic_version'
            """
        ).fetchall()
        if rows:
            conn.execute(f"TRUNCATE {', '.join(r[0] for r in rows)} RESTART IDENTITY CASCADE")
    return test_database_url


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection[TupleRow]]:
    """Autocommit connection to the emptied test DB."""
    with psycopg.connect(db_url, autocommit=True) as connection:
        yield connection

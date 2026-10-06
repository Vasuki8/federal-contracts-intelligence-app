"""Helpers for running and inspecting Alembic migrations from Python."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors

from pipeline.db import connect
from pipeline.settings import REPO_ROOT

ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    """Alembic config for programmatic use. `database_url` overrides DATABASE_URL."""
    config = Config(ALEMBIC_INI)
    config.attributes["configure_logger"] = False
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(database_url: str) -> str | None:
    """The revision the database is at, or None if no migration has run."""
    with connect(database_url) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except errors.UndefinedTable:
            return None
    return str(row[0]) if row else None

"""Database connection helpers."""

import psycopg
from psycopg.rows import TupleRow

_SQLALCHEMY_DRIVER_PREFIX = "postgresql+psycopg://"
_PLAIN_PREFIXES = ("postgresql://", "postgres://")


def sqlalchemy_url(database_url: str) -> str:
    """Return `database_url` with the psycopg (v3) driver that SQLAlchemy and Alembic need."""
    if database_url.startswith(_SQLALCHEMY_DRIVER_PREFIX):
        return database_url
    for prefix in _PLAIN_PREFIXES:
        if database_url.startswith(prefix):
            return _SQLALCHEMY_DRIVER_PREFIX + database_url.removeprefix(prefix)
    raise ValueError("Database URL must start with postgresql:// or postgres://")


def connect(database_url: str) -> psycopg.Connection[TupleRow]:
    return psycopg.connect(database_url, connect_timeout=5)


def server_version(database_url: str) -> str:
    with connect(database_url) as conn:
        row = conn.execute("SHOW server_version").fetchone()
    return str(row[0]) if row else "unknown"

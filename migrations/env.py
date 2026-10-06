"""Alembic environment. Migrations are hand-written; there are no ORM models."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from pipeline.db import sqlalchemy_url
from pipeline.settings import get_settings

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    """URL passed in by `pipeline.migrate.alembic_config`, else DATABASE_URL."""
    override = config.attributes.get("database_url")
    if isinstance(override, str):
        return override
    return get_settings().require_database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=sqlalchemy_url(_database_url()),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(_database_url()), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

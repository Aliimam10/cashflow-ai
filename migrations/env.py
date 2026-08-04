"""Alembic migration environment for the local SQLite database."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from cashflow_ai.config import load_settings
from cashflow_ai.persistence import models
from cashflow_ai.persistence.base import Base

config = context.config
if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)

configured_url = config.get_main_option("sqlalchemy.url")
if not configured_url:
    config.set_main_option("sqlalchemy.url", load_settings().database_url)

target_metadata = Base.metadata
assert models.UserProfileRecord.__tablename__ in target_metadata.tables


def run_migrations_offline() -> None:
    """Run migrations without opening a DB-API connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using an application-configured SQLite connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

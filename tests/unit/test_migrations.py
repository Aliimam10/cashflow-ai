"""Tests for the complete Alembic migration lifecycle."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select

from cashflow_ai.persistence import Base, create_sqlite_engine
from cashflow_ai.persistence.models import CategoryRecord, FinancialRoleRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "accounts",
    "anomaly_alerts",
    "balance_snapshots",
    "budgets",
    "categories",
    "category_corrections",
    "financial_roles",
    "forecast_runs",
    "import_batches",
    "import_contexts",
    "model_metadata",
    "raw_transactions",
    "recurring_series",
    "savings_goals",
    "scenarios",
    "statement_coverages",
    "user_flags",
    "user_profiles",
    "verified_transactions",
}


def migration_config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def migrated_engine(database_path: Path) -> Engine:
    return create_sqlite_engine(f"sqlite:///{database_path}")


def test_upgrade_creates_every_table_and_seeded_lookup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    config = migration_config(database_path)

    command.upgrade(config, "head")
    engine = migrated_engine(database_path)

    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    with engine.connect() as connection:
        categories = connection.execute(select(CategoryRecord.id)).scalars().all()
        roles = connection.execute(select(FinancialRoleRecord.id)).scalars().all()
    assert "needs_review" in categories
    assert "unknown" in roles


def test_migration_matches_model_metadata_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "roundtrip.db"
    config = migration_config(database_path)
    command.upgrade(config, "head")
    engine = migrated_engine(database_path)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []

    command.downgrade(config, "base")
    remaining = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES.isdisjoint(remaining)

    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES

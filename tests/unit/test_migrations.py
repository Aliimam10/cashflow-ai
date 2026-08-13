"""Tests for the complete Alembic migration lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text

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
    "category_decisions",
    "financial_roles",
    "financial_role_audits",
    "financial_role_suggestions",
    "forecast_runs",
    "import_batches",
    "import_contexts",
    "model_metadata",
    "personal_category_rules",
    "raw_transactions",
    "recurring_series",
    "recurring_payment_candidates",
    "recurring_payment_members",
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


def test_confirmed_import_migration_preserves_existing_and_downgrades_new_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "data-preservation.db"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    engine = migrated_engine(database_path)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles "
                "(id, display_name, base_currency, timezone, created_at, updated_at) "
                "VALUES "
                "('profile-1', 'Synthetic User', 'GBP', 'UTC', "
                "'2026-08-10 00:00:00', '2026-08-10 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_profile_id, name, account_type, currency, "
                "institution_label, is_active, created_at) VALUES "
                "('account-1', 'profile-1', 'Synthetic Account', 'current', "
                "'GBP', NULL, 1, '2026-08-10 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO import_batches "
                "(id, account_id, source_type, source_filename, file_hash, "
                "mime_type, byte_size, verification_status, imported_at) VALUES "
                "('batch-1', 'account-1', 'csv', 'synthetic.csv', :file_hash, "
                "'text/csv', 10, 'verified', '2026-08-10 00:00:00')"
            ),
            {"file_hash": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO raw_transactions "
                "(id, import_batch_id, source_type, source_row_number, page_number, "
                "page_record_number, raw_payload, original_date_text, "
                "original_description, original_amount_text, parser_name, "
                "parser_version, source_fingerprint, canonical_fingerprint, "
                "review_status, created_at) VALUES "
                "('raw-1', 'batch-1', 'csv', 2, NULL, NULL, '{}', '2026-07-01', "
                "'Synthetic row', '-1.00', 'parser', '1.0', :source_hash, "
                ":canonical_hash, 'confirmed', '2026-08-10 00:00:00')"
            ),
            {"source_hash": "b" * 64, "canonical_hash": "c" * 64},
        )

    command.upgrade(config, "head")
    with engine.begin() as connection:
        existing_issues = connection.scalar(
            text("SELECT issues_json FROM raw_transactions WHERE id = 'raw-1'")
        )
        assert json.loads(existing_issues) == []
        connection.execute(
            text(
                "INSERT INTO raw_transactions "
                "(id, import_batch_id, source_type, source_row_number, page_number, "
                "page_record_number, raw_payload, original_date_text, "
                "original_description, original_amount_text, parser_name, "
                "parser_version, source_fingerprint, canonical_fingerprint, "
                "issues_json, review_status, created_at) VALUES "
                "('raw-2', 'batch-1', 'csv', 3, NULL, NULL, '{}', 'bad date', "
                "'Synthetic rejected row', '-2.00', 'parser', '1.0', :source_hash, "
                "NULL, '[{\"code\": \"invalid_date\"}]', 'rejected', "
                "'2026-08-10 00:00:00')"
            ),
            {"source_hash": "d" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(id, account_id, import_batch_id, balance, currency, as_of_date, "
                "recorded_at, source, verification_status) VALUES "
                "('balance-1', 'account-1', 'batch-1', 1000.00, 'GBP', "
                "'2026-07-01', '2026-08-10 00:00:00', 'statement_opening', "
                "'verified')"
            )
        )

    command.downgrade(config, "0001")
    with engine.connect() as connection:
        downgraded_hash = connection.scalar(
            text(
                "SELECT canonical_fingerprint FROM raw_transactions WHERE id = 'raw-2'"
            )
        )
        downgraded_source = connection.scalar(
            text("SELECT source FROM balance_snapshots WHERE id = 'balance-1'")
        )
        raw_columns = {
            column["name"]
            for column in inspect(connection).get_columns("raw_transactions")
        }
    assert downgraded_hash == "d" * 64
    assert downgraded_source == "statement_closing"
    assert "issues_json" not in raw_columns

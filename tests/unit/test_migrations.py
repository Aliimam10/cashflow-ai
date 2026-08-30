"""Tests for the complete Alembic migration lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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


def as_utc(value: object) -> datetime:
    """Parse SQLite's timezone-naive UTC representation for boundary assertions."""
    parsed = datetime.fromisoformat(str(value))
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


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


def test_fresh_recurrence_hardening_upgrade_adds_no_synthetic_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fresh-recurrence-hardening.db"
    config = migration_config(database_path)

    command.upgrade(config, "head")
    engine = migrated_engine(database_path)

    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM recurring_payment_candidates"))
            == 0
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM recurring_payment_members"))
            == 0
        )
        candidate_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns(
                "recurring_payment_candidates"
            )
        }
        member_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("recurring_payment_members")
        }

    assert candidate_columns["knowledge_cutoff_at"]["nullable"] is False
    assert member_columns["identified_at"]["nullable"] is False


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


def test_recurrence_hardening_migration_backfills_identity_and_member_time(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recurrence-hardening.db"
    config = migration_config(database_path)
    command.upgrade(config, "0005")
    engine = migrated_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles "
                "(id, display_name, base_currency, timezone, created_at, updated_at) "
                "VALUES ('profile-r', 'Synthetic User', 'GBP', 'UTC', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_profile_id, name, account_type, currency, "
                "institution_label, is_active, created_at) VALUES "
                "('account-r', 'profile-r', 'Synthetic Account', 'current', "
                "'GBP', NULL, 1, '2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO import_batches "
                "(id, account_id, source_type, source_filename, file_hash, "
                "mime_type, byte_size, verification_status, imported_at) VALUES "
                "('batch-r', 'account-r', 'csv', 'synthetic.csv', :file_hash, "
                "'text/csv', 10, 'verified', '2026-03-02 00:00:00')"
            ),
            {"file_hash": "e" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO raw_transactions "
                "(id, import_batch_id, source_type, source_row_number, page_number, "
                "page_record_number, raw_payload, original_date_text, "
                "original_description, original_amount_text, parser_name, "
                "parser_version, source_fingerprint, canonical_fingerprint, "
                "issues_json, review_status, created_at) VALUES "
                "('raw-r', 'batch-r', 'csv', 2, NULL, NULL, '{}', '2026-03-05', "
                "'Synthetic Utility', '-20.00', 'parser', '1.0', :source_hash, "
                ":canonical_hash, '[]', 'confirmed', '2026-03-02 00:00:00')"
            ),
            {"source_hash": "f" * 64, "canonical_hash": "1" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO verified_transactions "
                "(id, raw_transaction_id, account_id, transaction_date, "
                "posting_date, description, merchant, amount, balance_after, "
                "currency, external_id, transaction_type, direction, category_id, "
                "financial_role_id, verified_at) VALUES "
                "('transaction-r', 'raw-r', 'account-r', '2026-03-05', "
                "'2026-03-05', 'Synthetic Utility', 'Synthetic Utility', -20.00, "
                "NULL, 'GBP', 'external-r', 'synthetic', 'outflow', NULL, "
                "'refund', '2026-03-02 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO financial_role_audits "
                "(id, verified_transaction_id, previous_role_id, new_role_id, "
                "suggestion_id, source, changed_at) VALUES "
                "('audit-r-before', 'transaction-r', 'unknown', 'expense', NULL, "
                "'user_override', '2026-03-01 00:00:00'), "
                "('audit-r-after', 'transaction-r', 'expense', 'refund', NULL, "
                "'user_override', '2026-03-03 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO recurring_payment_candidates "
                "(id, account_id, recurring_series_id, merchant_group, "
                "expected_amount, frequency, interval_days, next_expected_date, "
                "confidence, covered_missed_count, status, detected_at, reviewed_at) "
                "VALUES ('candidate-r', 'account-r', NULL, 'synthetic utility', "
                "-20.00, 'monthly', 30, '2026-04-01', 0.9, 0, 'cancelled', "
                "'2026-03-02 00:00:00', '2026-03-01 00:00:00'), "
                "('candidate-unknown', 'account-r', NULL, 'synthetic utility', "
                "-20.00, 'monthly', 30, '2026-03-31', 0.8, 0, 'pending', "
                "'2026-02-28 00:00:00', NULL), "
                "('candidate-duplicate', 'account-r', NULL, 'synthetic utility', "
                "-21.00, 'monthly', 30, '2026-03-31', 0.7, 0, 'cancelled', "
                "'2026-02-28 00:00:00', '2026-02-27 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO recurring_payment_members "
                "(candidate_id, verified_transaction_id) "
                "VALUES ('candidate-r', 'transaction-r'), "
                "('candidate-unknown', 'transaction-r')"
            )
        )

    upgrade_started_at = datetime.now(UTC)
    command.upgrade(config, "0006")
    upgrade_finished_at = datetime.now(UTC)
    with engine.connect() as connection:
        candidate = connection.execute(
            text(
                "SELECT currency, direction, financial_role_id, "
                "evidence_as_of_date, knowledge_cutoff_at, detected_at "
                "FROM recurring_payment_candidates WHERE id = 'candidate-r'"
            )
        ).one()
        identified_times = connection.execute(
            text(
                "SELECT candidate_id, verified_transaction_id, identified_at "
                "FROM recurring_payment_members ORDER BY candidate_id"
            )
        ).all()
        unknown_roles = connection.execute(
            text(
                "SELECT id, financial_role_id FROM recurring_payment_candidates "
                "WHERE id IN ('candidate-unknown', 'candidate-duplicate') "
                "ORDER BY id"
            )
        ).all()
        legacy_reviewed_at = connection.scalar(
            text(
                "SELECT reviewed_at FROM recurring_payment_candidates "
                "WHERE id = 'candidate-duplicate'"
            )
        )
        visible_before_migration = connection.scalar(
            text(
                "SELECT COUNT(*) FROM recurring_payment_candidates "
                "WHERE knowledge_cutoff_at <= :cutoff"
            ),
            {"cutoff": upgrade_started_at - timedelta(microseconds=1)},
        )
        visible_after_migration = connection.scalar(
            text(
                "SELECT COUNT(*) FROM recurring_payment_candidates "
                "WHERE knowledge_cutoff_at <= :cutoff"
            ),
            {"cutoff": upgrade_finished_at + timedelta(microseconds=1)},
        )
    assert tuple(candidate[:3]) == ("GBP", "outflow", "expense")
    assert str(candidate.evidence_as_of_date) == "2026-03-02"
    candidate_available_at = as_utc(candidate.knowledge_cutoff_at)
    assert upgrade_started_at <= candidate_available_at <= upgrade_finished_at
    assert [tuple(row[:2]) for row in identified_times] == [
        ("candidate-r", "transaction-r"),
        ("candidate-unknown", "transaction-r"),
    ]
    assert {as_utc(row.identified_at) for row in identified_times} == {
        candidate_available_at
    }
    assert visible_before_migration == 0
    assert visible_after_migration == 3
    assert str(candidate.detected_at).startswith("2026-03-02")
    assert [tuple(row) for row in unknown_roles] == [
        ("candidate-duplicate", "unknown"),
        ("candidate-unknown", "unknown"),
    ]
    assert str(legacy_reviewed_at).startswith("2026-02-27")

    command.downgrade(config, "0005")
    with engine.connect() as connection:
        candidate_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                "recurring_payment_candidates"
            )
        }
        member_columns = {
            column["name"]
            for column in inspect(connection).get_columns("recurring_payment_members")
        }
        member_count = connection.scalar(
            text("SELECT COUNT(*) FROM recurring_payment_members")
        )
    assert "knowledge_cutoff_at" not in candidate_columns
    assert "identified_at" not in member_columns
    assert member_count == 2

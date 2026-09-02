"""Tests for the complete Alembic migration lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

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
    "derived_result_states",
    "financial_data_revisions",
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


def test_model_registry_migration_preserves_legacy_rows_and_enforces_activation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "model-registry.db"
    config = migration_config(database_path)
    command.upgrade(config, "0006")
    engine = migrated_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO model_metadata "
                "(id, model_name, model_version, task, artifact_path, "
                "training_cutoff, metrics_json, parameters_json, created_at) "
                "VALUES ('legacy-model', 'legacy_forecast', 'v1', "
                "'cash_flow_forecasting', NULL, '2025-06-30', "
                "'{\"mae\": 10}', '{\"seed\": 7}', '2025-07-01 00:00:00')"
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        migrated = connection.execute(
            text(
                "SELECT model_type, training_start_date, training_end_date, "
                "feature_schema_version, feature_names_json, "
                "metadata_format_version, activation_eligible, is_active, "
                "metrics_json, parameters_json FROM model_metadata "
                "WHERE id = 'legacy-model'"
            )
        ).one()
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("model_metadata")
        }
        indexes = {
            item["name"]: item
            for item in inspect(connection).get_indexes("model_metadata")
        }

    assert tuple(migrated[:4]) == (
        "legacy_forecast",
        "2025-06-30",
        "2025-06-30",
        "legacy_unknown",
    )
    assert json.loads(migrated.feature_names_json) == []
    assert tuple(migrated[5:8]) == ("legacy-0", 0, 0)
    assert json.loads(migrated.metrics_json) == {"mae": 10}
    assert json.loads(migrated.parameters_json) == {"seed": 7}
    for required in (
        "model_type",
        "training_start_date",
        "training_end_date",
        "feature_schema_version",
        "feature_names_json",
        "metadata_format_version",
        "activation_eligible",
        "is_active",
    ):
        assert columns[required]["nullable"] is False
    assert indexes["uq_model_metadata_active_task"]["unique"] == 1

    insert_current = text(
        "INSERT INTO model_metadata "
        "(id, model_name, model_type, model_version, task, artifact_path, "
        "training_cutoff, training_start_date, training_end_date, "
        "feature_schema_version, feature_names_json, taxonomy_version, "
        "metrics_json, parameters_json, metadata_format_version, "
        "activation_eligible, is_active, activated_at, created_at) VALUES "
        "(:id, :name, 'hist_gradient_boosting', :version, :task, NULL, "
        "'2025-06-30', '2025-01-01', '2025-06-30', 'weekly_v1', "
        "'[\"lag_1\"]', NULL, '{\"metrics\": []}', "
        "'{\"parameters\": []}', '1.0', :eligible, :active, "
        ":activated_at, '2025-07-01 00:00:00')"
    )
    with engine.begin() as connection:
        connection.execute(
            insert_current,
            {
                "id": "active-1",
                "name": "forecast_a",
                "version": "v1",
                "task": "cash_flow_forecasting",
                "eligible": 1,
                "active": 1,
                "activated_at": "2025-07-02 00:00:00",
            },
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert_current,
            {
                "id": "active-2",
                "name": "forecast_b",
                "version": "v1",
                "task": "cash_flow_forecasting",
                "eligible": 1,
                "active": 1,
                "activated_at": "2025-07-02 00:00:00",
            },
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert_current,
            {
                "id": "ineligible-active",
                "name": "anomaly_a",
                "version": "v1",
                "task": "transaction_anomaly_detection",
                "eligible": 0,
                "active": 1,
                "activated_at": "2025-07-02 00:00:00",
            },
        )

    command.downgrade(config, "0006")
    with engine.connect() as connection:
        downgraded = connection.execute(
            text(
                "SELECT model_name, metrics_json, parameters_json "
                "FROM model_metadata WHERE id = 'legacy-model'"
            )
        ).one()
        downgraded_columns = {
            column["name"]
            for column in inspect(connection).get_columns("model_metadata")
        }
    assert downgraded.model_name == "legacy_forecast"
    assert json.loads(downgraded.metrics_json) == {"mae": 10}
    assert json.loads(downgraded.parameters_json) == {"seed": 7}
    assert "model_type" not in downgraded_columns


def test_planning_type_migration_preserves_legacy_rows_and_blocks_lossy_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "planning-types.db"
    config = migration_config(database_path)
    command.upgrade(config, "0007")
    engine = migrated_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles "
                "(id, display_name, base_currency, timezone, created_at, updated_at) "
                "VALUES ('planning-profile', 'Fictional User', 'GBP', 'UTC', "
                "'2026-08-01 00:00:00', '2026-08-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_profile_id, name, account_type, currency, "
                "institution_label, is_active, created_at) VALUES "
                "('planning-account', 'planning-profile', 'Fictional Account', "
                "'current', 'GBP', NULL, 1, '2026-08-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO budgets "
                "(id, user_profile_id, category_id, period_start, period_end, "
                "amount_limit, currency) VALUES "
                "('legacy-budget', 'planning-profile', 'groceries', "
                "'2026-08-01', '2026-08-31', 200.00, 'GBP')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO savings_goals "
                "(id, account_id, name, target_amount, current_amount, "
                "target_date, created_at) VALUES "
                "('legacy-goal', 'planning-account', 'Fictional Goal', "
                "500.00, 100.00, '2026-12-31', '2026-08-01 00:00:00')"
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        budget_type = connection.scalar(
            text("SELECT budget_type FROM budgets WHERE id = 'legacy-budget'")
        )
        goal_type = connection.scalar(
            text("SELECT goal_type FROM savings_goals WHERE id = 'legacy-goal'")
        )
        budget_columns = {
            item["name"]: item for item in inspect(connection).get_columns("budgets")
        }
        goal_indexes = {
            item["name"]: item
            for item in inspect(connection).get_indexes("savings_goals")
        }
    assert budget_type == "monthly_category"
    assert goal_type == "savings_target"
    assert budget_columns["budget_type"]["nullable"] is False
    assert budget_columns["category_id"]["nullable"] is True
    assert goal_indexes["uq_savings_goals_minimum_balance"]["unique"] == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO budgets "
                "(id, user_profile_id, budget_type, category_id, period_start, "
                "period_end, amount_limit, currency) VALUES "
                "('weekly-budget', 'planning-profile', 'weekly_discretionary', "
                "NULL, '2026-08-03', '2026-08-09', 50.00, 'GBP')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO savings_goals "
                "(id, account_id, goal_type, name, target_amount, current_amount, "
                "target_date, created_at) VALUES "
                "('minimum-goal', 'planning-account', 'minimum_balance', "
                "'Fictional Floor', 250.00, 0.00, NULL, "
                "'2026-08-01 00:00:00')"
            )
        )

    with pytest.raises(RuntimeError, match="cannot downgrade planning types"):
        command.downgrade(config, "0007")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM budgets WHERE id = 'weekly-budget'")
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM savings_goals WHERE id = 'minimum-goal'")
            )
            == 1
        )

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM budgets WHERE id = 'weekly-budget'"))
        connection.execute(text("DELETE FROM savings_goals WHERE id = 'minimum-goal'"))
    command.downgrade(config, "0007")
    with engine.connect() as connection:
        legacy_budget = connection.execute(
            text(
                "SELECT category_id, amount_limit FROM budgets "
                "WHERE id = 'legacy-budget'"
            )
        ).one()
        legacy_goal = connection.execute(
            text(
                "SELECT target_amount, current_amount FROM savings_goals "
                "WHERE id = 'legacy-goal'"
            )
        ).one()
        budget_columns = {
            item["name"]: item for item in inspect(connection).get_columns("budgets")
        }
        goal_columns = {
            item["name"] for item in inspect(connection).get_columns("savings_goals")
        }
    assert tuple(legacy_budget) == ("groceries", 200)
    assert tuple(legacy_goal) == (500, 100)
    assert "budget_type" not in budget_columns
    assert budget_columns["category_id"]["nullable"] is False
    assert "goal_type" not in goal_columns


def test_derived_freshness_migration_is_additive_constrained_and_source_safe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "derived-freshness.db"
    config = migration_config(database_path)
    command.upgrade(config, "0008")
    engine = migrated_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_profiles "
                "(id, display_name, base_currency, timezone, created_at, updated_at) "
                "VALUES ('revision-profile', 'Fictional User', 'GBP', 'UTC', "
                "'2026-09-01 00:00:00', '2026-09-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_profile_id, name, account_type, currency, "
                "institution_label, is_active, created_at) VALUES "
                "('revision-account', 'revision-profile', 'Fictional Account', "
                "'current', 'GBP', NULL, 1, '2026-09-01 00:00:00')"
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM accounts")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM financial_data_revisions"))
            == 0
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM derived_result_states")) == 0
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO financial_data_revisions "
                "(account_id, revision, last_change_type, changed_at) VALUES "
                "('revision-account', 1, 'statement_added', "
                "'2026-09-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO derived_result_states "
                "(id, account_id, output_type, status, required_revision, "
                "computed_revision, generated_at, invalidated_at, invalidated_by) "
                "VALUES ('revision-state', 'revision-account', 'analytics', "
                "'unavailable', 1, NULL, NULL, '2026-09-01 12:00:00', "
                "'statement_added')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO derived_result_states "
                "(id, account_id, output_type, status, required_revision, "
                "computed_revision, generated_at, invalidated_at, invalidated_by) "
                "VALUES ('bad-state', 'revision-account', 'forecasts', "
                "'current', 1, 0, '2026-09-01 12:00:00', NULL, NULL)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE financial_data_revisions "
                "SET last_change_type = 'invented_change' "
                "WHERE account_id = 'revision-account'"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE derived_result_states "
                "SET invalidated_by = 'invented_change' "
                "WHERE id = 'revision-state'"
            )
        )

    command.downgrade(config, "0008")
    tables = set(inspect(engine).get_table_names())
    assert "financial_data_revisions" not in tables
    assert "derived_result_states" not in tables
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM accounts")) == 1

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM accounts")) == 1

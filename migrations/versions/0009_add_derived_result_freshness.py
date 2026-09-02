"""Track source revisions and derived-result freshness.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cashflow_ai.persistence.base import UTCDateTime

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create data-minimised revision and freshness metadata tables."""
    op.create_table(
        "financial_data_revisions",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_change_type", sa.String(length=50), nullable=True),
        sa.Column("changed_at", UTCDateTime(), nullable=True),
        sa.CheckConstraint("revision >= 0", name="ck_financial_data_revisions_value"),
        sa.CheckConstraint(
            "last_change_type IS NULL OR last_change_type IN "
            "('ocr_corrected', 'transaction_amount_changed', "
            "'financial_role_changed', 'category_changed', 'transfer_confirmed', "
            "'statement_added', 'import_deleted', 'current_balance_changed')",
            name="ck_financial_data_revisions_change_type",
        ),
        sa.CheckConstraint(
            "(revision = 0 AND last_change_type IS NULL AND changed_at IS NULL) OR "
            "(revision > 0 AND last_change_type IS NOT NULL "
            "AND changed_at IS NOT NULL)",
            name="ck_financial_data_revisions_origin",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "derived_result_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("output_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("required_revision", sa.Integer(), nullable=False),
        sa.Column("computed_revision", sa.Integer(), nullable=True),
        sa.Column("generated_at", UTCDateTime(), nullable=True),
        sa.Column("invalidated_at", UTCDateTime(), nullable=True),
        sa.Column("invalidated_by", sa.String(length=50), nullable=True),
        sa.CheckConstraint(
            "output_type IN ('analytics', 'recurring_series', 'anomaly_alerts', "
            "'budgets', 'forecasts', 'scenarios', "
            "'model_performance_comparisons')",
            name="ck_derived_result_states_output_type",
        ),
        sa.CheckConstraint(
            "status IN ('unavailable', 'current', 'stale')",
            name="ck_derived_result_states_status",
        ),
        sa.CheckConstraint(
            "invalidated_by IS NULL OR invalidated_by IN "
            "('ocr_corrected', 'transaction_amount_changed', "
            "'financial_role_changed', 'category_changed', 'transfer_confirmed', "
            "'statement_added', 'import_deleted', 'current_balance_changed')",
            name="ck_derived_result_states_change_type",
        ),
        sa.CheckConstraint(
            "required_revision >= 0 AND "
            "(computed_revision IS NULL OR "
            "(computed_revision >= 0 AND computed_revision <= required_revision))",
            name="ck_derived_result_states_revisions",
        ),
        sa.CheckConstraint(
            "(status = 'current' AND computed_revision = required_revision "
            "AND generated_at IS NOT NULL AND invalidated_at IS NULL "
            "AND invalidated_by IS NULL) OR "
            "(status = 'stale' AND computed_revision IS NOT NULL "
            "AND computed_revision < required_revision AND generated_at IS NOT NULL "
            "AND invalidated_at IS NOT NULL AND invalidated_by IS NOT NULL) OR "
            "(status = 'unavailable' AND computed_revision IS NULL "
            "AND generated_at IS NULL AND "
            "((required_revision = 0 AND invalidated_at IS NULL "
            "AND invalidated_by IS NULL) OR "
            "(required_revision > 0 AND invalidated_at IS NOT NULL "
            "AND invalidated_by IS NOT NULL)))",
            name="ck_derived_result_states_shape",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "output_type", name="uq_derived_result_states_scope"
        ),
    )
    with op.batch_alter_table("derived_result_states", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_derived_result_states_account_id"),
            ["account_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove freshness metadata without changing any financial source data."""
    with op.batch_alter_table("derived_result_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_derived_result_states_account_id"))
    op.drop_table("derived_result_states")
    op.drop_table("financial_data_revisions")

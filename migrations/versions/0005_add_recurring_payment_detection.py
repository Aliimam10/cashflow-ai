"""Add recurring-payment candidates and evidence membership.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive recurrence detection tables."""
    op.create_table(
        "recurring_payment_candidates",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("recurring_series_id", sa.String(36), nullable=True),
        sa.Column("merchant_group", sa.String(500), nullable=False),
        sa.Column("expected_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("frequency", sa.String(20), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("next_expected_date", sa.Date(), nullable=False),
        sa.Column("confidence", sa.Numeric(7, 6), nullable=False),
        sa.Column("covered_missed_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "frequency IN ('weekly', 'fortnightly', 'monthly', 'quarterly', 'annual')",
            name="ck_recurring_candidates_frequency",
        ),
        sa.CheckConstraint(
            "interval_days > 0", name="ck_recurring_candidates_interval"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_recurring_candidates_confidence",
        ),
        sa.CheckConstraint(
            "covered_missed_count >= 0", name="ck_recurring_candidates_missed"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_recurring_candidates_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status != 'pending' AND reviewed_at IS NOT NULL)",
            name="ck_recurring_candidates_reviewed",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["recurring_series_id"], ["recurring_series.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recurring_payment_candidates_account_id"),
        "recurring_payment_candidates",
        ["account_id"],
    )
    op.create_table(
        "recurring_payment_members",
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("verified_transaction_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["recurring_payment_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["verified_transaction_id"],
            ["verified_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("candidate_id", "verified_transaction_id"),
    )


def downgrade() -> None:
    """Remove only Commit 21 recurrence detection state."""
    op.drop_table("recurring_payment_members")
    op.drop_index(
        op.f("ix_recurring_payment_candidates_account_id"),
        table_name="recurring_payment_candidates",
    )
    op.drop_table("recurring_payment_candidates")

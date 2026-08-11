"""Add financial-role suggestions and immutable audit history.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive role-suggestion and role-audit tables."""
    op.create_table(
        "financial_role_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_key", sa.String(length=64), nullable=False),
        sa.Column("verified_transaction_id", sa.String(length=36), nullable=False),
        sa.Column("counterpart_transaction_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("suggested_role_id", sa.String(length=50), nullable=False),
        sa.Column("counterpart_role_id", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('transfer', 'refund', 'reimbursement')",
            name="ck_financial_role_suggestions_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_financial_role_suggestions_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_financial_role_suggestions_confidence",
        ),
        sa.CheckConstraint(
            "counterpart_transaction_id IS NULL OR "
            "counterpart_transaction_id != verified_transaction_id",
            name="ck_financial_role_suggestions_counterpart",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status != 'pending' AND reviewed_at IS NOT NULL)",
            name="ck_financial_role_suggestions_reviewed_at",
        ),
        sa.CheckConstraint(
            "(kind = 'transfer' AND suggested_role_id IN "
            "('transfer_in', 'transfer_out') AND "
            "((counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL) "
            "OR (counterpart_transaction_id IS NOT NULL AND "
            "counterpart_role_id IN ('transfer_in', 'transfer_out') AND "
            "counterpart_role_id != suggested_role_id))) OR "
            "(kind = 'refund' AND suggested_role_id = 'refund' AND "
            "counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL) OR "
            "(kind = 'reimbursement' AND suggested_role_id = 'reimbursement' AND "
            "counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL)",
            name="ck_financial_role_suggestions_roles",
        ),
        sa.ForeignKeyConstraint(
            ["verified_transaction_id"],
            ["verified_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["counterpart_transaction_id"],
            ["verified_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suggested_role_id"], ["financial_roles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["counterpart_role_id"], ["financial_roles.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suggestion_key"),
    )
    with op.batch_alter_table("financial_role_suggestions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_financial_role_suggestions_counterpart_transaction_id"),
            ["counterpart_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_financial_role_suggestions_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_financial_role_suggestions_verified_transaction_id"),
            ["verified_transaction_id"],
            unique=False,
        )

    op.create_table(
        "financial_role_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("verified_transaction_id", sa.String(length=36), nullable=False),
        sa.Column("previous_role_id", sa.String(length=50), nullable=False),
        sa.Column("new_role_id", sa.String(length=50), nullable=False),
        sa.Column("suggestion_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('user_confirmation', 'user_override')",
            name="ck_financial_role_audits_source",
        ),
        sa.CheckConstraint(
            "previous_role_id != new_role_id",
            name="ck_financial_role_audits_changed",
        ),
        sa.ForeignKeyConstraint(
            ["verified_transaction_id"],
            ["verified_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["previous_role_id"], ["financial_roles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["new_role_id"], ["financial_roles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["financial_role_suggestions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("financial_role_audits", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_financial_role_audits_verified_transaction_id"),
            ["verified_transaction_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove financial-role review tables without touching transactions."""
    with op.batch_alter_table("financial_role_audits", schema=None) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_financial_role_audits_verified_transaction_id")
        )
    op.drop_table("financial_role_audits")

    with op.batch_alter_table("financial_role_suggestions", schema=None) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_financial_role_suggestions_verified_transaction_id")
        )
        batch_op.drop_index(batch_op.f("ix_financial_role_suggestions_status"))
        batch_op.drop_index(
            batch_op.f("ix_financial_role_suggestions_counterpart_transaction_id")
        )
    op.drop_table("financial_role_suggestions")

"""Add hybrid categorisation decisions and explicit personal rules.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive feedback and decision-audit tables."""
    op.create_table(
        "personal_category_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_profile_id", sa.String(length=36), nullable=False),
        sa.Column("category_id", sa.String(length=100), nullable=False),
        sa.Column("merchant", sa.String(length=500), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=True),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("description_contains", sa.String(length=500), nullable=True),
        sa.Column("minimum_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("maximum_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('inflow', 'outflow')",
            name="ck_personal_category_rules_direction",
        ),
        sa.CheckConstraint("priority >= 0", name="ck_personal_category_rules_priority"),
        sa.CheckConstraint(
            "minimum_amount IS NULL OR minimum_amount >= 0",
            name="ck_personal_category_rules_minimum",
        ),
        sa.CheckConstraint(
            "maximum_amount IS NULL OR maximum_amount >= 0",
            name="ck_personal_category_rules_maximum",
        ),
        sa.CheckConstraint(
            "minimum_amount IS NULL OR maximum_amount IS NULL "
            "OR maximum_amount >= minimum_amount",
            name="ck_personal_category_rules_range",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_profile_id"], ["user_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_personal_category_rules_user_profile_id"),
        "personal_category_rules",
        ["user_profile_id"],
        unique=False,
    )
    op.create_table(
        "category_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("verified_transaction_id", sa.String(length=36), nullable=False),
        sa.Column("category_id", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Numeric(7, 6), nullable=True),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("rule_id", sa.String(length=100), nullable=True),
        sa.Column("taxonomy_version", sa.String(length=50), nullable=False),
        sa.Column("rule_set_version", sa.String(length=50), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "source IN ('transaction_decision', 'personal_rule', "
            "'merchant_mapping', 'keyword_rule', 'ml_model', 'needs_review')",
            name="ck_category_decisions_source",
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'pending_review', 'superseded')",
            name="ck_category_decisions_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_category_decisions_confidence",
        ),
        sa.CheckConstraint(
            "(source = 'ml_model' AND confidence IS NOT NULL "
            "AND model_version IS NOT NULL) OR "
            "(source != 'ml_model' AND confidence IS NULL "
            "AND model_version IS NULL)",
            name="ck_category_decisions_model_evidence",
        ),
        sa.CheckConstraint(
            "(status = 'superseded' AND reviewed_at IS NOT NULL) OR "
            "(status != 'superseded' AND reviewed_at IS NULL)",
            name="ck_category_decisions_reviewed",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["verified_transaction_id"],
            ["verified_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_category_decisions_verified_transaction_id"),
        "category_decisions",
        ["verified_transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove Commit 20 tables without touching imported transactions."""
    op.drop_index(
        op.f("ix_category_decisions_verified_transaction_id"),
        table_name="category_decisions",
    )
    op.drop_table("category_decisions")
    op.drop_index(
        op.f("ix_personal_category_rules_user_profile_id"),
        table_name="personal_category_rules",
    )
    op.drop_table("personal_category_rules")

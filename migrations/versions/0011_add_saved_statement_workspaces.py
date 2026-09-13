"""Add data-minimised storage for finalized statement workspaces.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cashflow_ai.persistence.base import UTCDateTime

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create isolated saved-workspace tables without changing source tables."""
    op.create_table(
        "saved_workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_name", sa.String(length=100), nullable=False),
        sa.Column("retention_mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("finalized_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "length(trim(account_name)) > 0", name="ck_saved_workspaces_account_name"
        ),
        sa.CheckConstraint(
            "retention_mode = 'saved'", name="ck_saved_workspaces_retention"
        ),
        sa.CheckConstraint("status = 'finalized'", name="ck_saved_workspaces_status"),
        sa.CheckConstraint("revision >= 1", name="ck_saved_workspaces_revision"),
        sa.CheckConstraint(
            "finalized_at >= created_at", name="ck_saved_workspaces_timestamps"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "saved_workspace_transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("merchant", sa.String(length=500), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("transaction_type", sa.String(length=255), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("category_id", sa.String(length=100), nullable=True),
        sa.Column("financial_role", sa.String(length=30), nullable=False),
        sa.CheckConstraint(
            "position >= 1", name="ck_saved_workspace_transactions_position"
        ),
        sa.CheckConstraint(
            "length(trim(description)) > 0",
            name="ck_saved_workspace_transactions_description",
        ),
        sa.CheckConstraint(
            "amount != 0", name="ck_saved_workspace_transactions_amount"
        ),
        sa.CheckConstraint(
            "currency = 'GBP'", name="ck_saved_workspace_transactions_currency"
        ),
        sa.CheckConstraint(
            "direction IN ('inflow', 'outflow')",
            name="ck_saved_workspace_transactions_direction",
        ),
        sa.CheckConstraint(
            "(amount > 0 AND direction = 'inflow') OR "
            "(amount < 0 AND direction = 'outflow')",
            name="ck_saved_workspace_transactions_signed_direction",
        ),
        sa.CheckConstraint(
            "financial_role IN ('income', 'expense', 'transfer_in', "
            "'transfer_out', 'refund', 'reimbursement', 'cash_withdrawal', "
            "'excluded', 'unknown')",
            name="ck_saved_workspace_transactions_financial_role",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["saved_workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "position", name="uq_saved_workspace_transactions_order"
        ),
    )
    with op.batch_alter_table("saved_workspace_transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_saved_workspace_transactions_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
    op.create_table(
        "saved_workspace_coverage",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("statement_start_date", sa.Date(), nullable=False),
        sa.Column("statement_end_date", sa.Date(), nullable=False),
        sa.Column("coverage_status", sa.String(length=20), nullable=False),
        sa.Column("missing_periods_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "statement_end_date >= statement_start_date",
            name="ck_saved_workspace_coverage_dates",
        ),
        sa.CheckConstraint(
            "coverage_status IN "
            "('complete', 'partial', 'gapped', 'overlapping', 'unknown')",
            name="ck_saved_workspace_coverage_status",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["saved_workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "saved_workspace_balances",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "currency = 'GBP'", name="ck_saved_workspace_balances_currency"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["saved_workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def downgrade() -> None:
    """Remove empty workspace tables, refusing to erase saved user data."""
    connection = op.get_bind()
    saved_count = connection.scalar(sa.text("SELECT COUNT(*) FROM saved_workspaces"))
    if saved_count:
        raise RuntimeError("cannot downgrade while finalized workspaces are saved")
    op.drop_table("saved_workspace_balances")
    op.drop_table("saved_workspace_coverage")
    with op.batch_alter_table("saved_workspace_transactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_saved_workspace_transactions_workspace_id"))
    op.drop_table("saved_workspace_transactions")
    op.drop_table("saved_workspaces")

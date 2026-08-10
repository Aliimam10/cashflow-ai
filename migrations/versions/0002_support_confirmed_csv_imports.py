"""Support quarantined rows and opening statement balances.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow rejected raw rows and both statement balance endpoints."""
    with op.batch_alter_table("raw_transactions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "issues_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.alter_column(
            "canonical_fingerprint",
            existing_type=sa.String(length=64),
            nullable=True,
        )

    with op.batch_alter_table("balance_snapshots", schema=None) as batch_op:
        batch_op.drop_constraint("ck_balance_snapshots_source", type_="check")
        batch_op.create_check_constraint(
            "ck_balance_snapshots_source",
            "source IN "
            "('statement_opening', 'statement_closing', 'running_balance', 'manual')",
        )


def downgrade() -> None:
    """Return to the schema before confirmed CSV quarantine support."""
    op.execute(
        sa.text(
            "UPDATE balance_snapshots SET source = 'statement_closing' "
            "WHERE source = 'statement_opening'"
        )
    )
    with op.batch_alter_table("balance_snapshots", schema=None) as batch_op:
        batch_op.drop_constraint("ck_balance_snapshots_source", type_="check")
        batch_op.create_check_constraint(
            "ck_balance_snapshots_source",
            "source IN ('statement_closing', 'running_balance', 'manual')",
        )

    op.execute(
        sa.text(
            "UPDATE raw_transactions SET canonical_fingerprint = source_fingerprint "
            "WHERE canonical_fingerprint IS NULL"
        )
    )
    with op.batch_alter_table("raw_transactions", schema=None) as batch_op:
        batch_op.alter_column(
            "canonical_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.drop_column("issues_json")

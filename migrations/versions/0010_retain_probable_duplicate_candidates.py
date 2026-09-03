"""Retain canonical drafts needed for probable-duplicate review.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add an optional private canonical snapshot to preserved raw rows."""
    with op.batch_alter_table("raw_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("candidate_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop snapshots only when no unresolved review would lose its keep path."""
    connection = op.get_bind()
    unresolved = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM raw_transactions "
            "WHERE review_status = 'needs_review' AND candidate_json IS NOT NULL"
        )
    )
    if unresolved:
        raise RuntimeError(
            "cannot downgrade while probable duplicate candidates need review"
        )
    with op.batch_alter_table("raw_transactions", schema=None) as batch_op:
        batch_op.drop_column("candidate_json")

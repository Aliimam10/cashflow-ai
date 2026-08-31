"""Add the lightweight model registry lifecycle fields.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend existing metadata without activating or reinterpreting legacy rows."""
    with op.batch_alter_table("model_metadata", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model_type", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("training_start_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("training_end_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("feature_schema_version", sa.String(50), nullable=True)
        )
        batch_op.add_column(sa.Column("feature_names_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("taxonomy_version", sa.String(50), nullable=True))
        batch_op.add_column(
            sa.Column("metadata_format_version", sa.String(20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("activation_eligible", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(sa.Column("is_active", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("activated_at", sa.DateTime(), nullable=True))

    op.get_bind().execute(
        sa.text(
            """
            UPDATE model_metadata
            SET model_type = model_name,
                training_start_date = COALESCE(training_cutoff, date(created_at)),
                training_end_date = COALESCE(training_cutoff, date(created_at)),
                feature_schema_version = 'legacy_unknown',
                feature_names_json = '[]',
                metadata_format_version = 'legacy-0',
                activation_eligible = 0,
                is_active = 0,
                activated_at = NULL
            """
        )
    )

    with op.batch_alter_table("model_metadata", schema=None) as batch_op:
        batch_op.alter_column(
            "model_type", existing_type=sa.String(100), nullable=False
        )
        batch_op.alter_column(
            "training_start_date", existing_type=sa.Date(), nullable=False
        )
        batch_op.alter_column(
            "training_end_date", existing_type=sa.Date(), nullable=False
        )
        batch_op.alter_column(
            "feature_schema_version", existing_type=sa.String(50), nullable=False
        )
        batch_op.alter_column(
            "feature_names_json", existing_type=sa.JSON(), nullable=False
        )
        batch_op.alter_column(
            "metadata_format_version", existing_type=sa.String(20), nullable=False
        )
        batch_op.alter_column(
            "activation_eligible", existing_type=sa.Boolean(), nullable=False
        )
        batch_op.alter_column("is_active", existing_type=sa.Boolean(), nullable=False)
        batch_op.create_check_constraint(
            "ck_model_metadata_training_dates",
            "training_end_date >= training_start_date",
        )
        batch_op.create_check_constraint(
            "ck_model_metadata_active_eligible",
            "is_active = 0 OR activation_eligible = 1",
        )
        batch_op.create_check_constraint(
            "ck_model_metadata_active_timestamp",
            "is_active = 0 OR activated_at IS NOT NULL",
        )
    op.create_index(
        "uq_model_metadata_active_task",
        "model_metadata",
        ["task"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    """Remove lifecycle fields while preserving the original metadata columns."""
    op.drop_index("uq_model_metadata_active_task", table_name="model_metadata")
    with op.batch_alter_table("model_metadata", schema=None) as batch_op:
        batch_op.drop_constraint("ck_model_metadata_active_timestamp", type_="check")
        batch_op.drop_constraint("ck_model_metadata_active_eligible", type_="check")
        batch_op.drop_constraint("ck_model_metadata_training_dates", type_="check")
        batch_op.drop_column("activated_at")
        batch_op.drop_column("is_active")
        batch_op.drop_column("activation_eligible")
        batch_op.drop_column("metadata_format_version")
        batch_op.drop_column("taxonomy_version")
        batch_op.drop_column("feature_names_json")
        batch_op.drop_column("feature_schema_version")
        batch_op.drop_column("training_end_date")
        batch_op.drop_column("training_start_date")
        batch_op.drop_column("model_type")

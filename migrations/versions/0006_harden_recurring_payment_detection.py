"""Harden recurring candidate identity and cutoff provenance.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the full series identity and auditable evidence cutoff.

    Legacy review timestamps and duplicate candidate identities are preserved. Since
    revision 0005 did not record when a candidate's derived identity or each member
    became known, the migration execution time is their conservative availability
    boundary. This prevents migrated evidence from appearing in pre-migration
    forecasts. Omitting new chronology and uniqueness constraints keeps this
    additive migration compatible with 0005 data.
    """
    legacy_available_at = datetime.now(UTC)
    with op.batch_alter_table("recurring_payment_candidates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("currency", sa.String(3), nullable=True))
        batch_op.add_column(sa.Column("direction", sa.String(10), nullable=True))
        batch_op.add_column(
            sa.Column("financial_role_id", sa.String(50), nullable=True)
        )
        batch_op.add_column(sa.Column("evidence_as_of_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("knowledge_cutoff_at", sa.DateTime(), nullable=True)
        )
    with op.batch_alter_table("recurring_payment_members", schema=None) as batch_op:
        batch_op.add_column(sa.Column("identified_at", sa.DateTime(), nullable=True))

    op.get_bind().execute(
        sa.text(
            """
            UPDATE recurring_payment_candidates
            SET currency = COALESCE(
                    (SELECT verified_transactions.currency
                     FROM recurring_payment_members
                     JOIN verified_transactions
                       ON verified_transactions.id =
                          recurring_payment_members.verified_transaction_id
                     WHERE recurring_payment_members.candidate_id =
                           recurring_payment_candidates.id
                     ORDER BY verified_transactions.transaction_date,
                              verified_transactions.id
                     LIMIT 1),
                    'GBP'
                ),
                direction = COALESCE(
                    (SELECT verified_transactions.direction
                     FROM recurring_payment_members
                     JOIN verified_transactions
                       ON verified_transactions.id =
                          recurring_payment_members.verified_transaction_id
                     WHERE recurring_payment_members.candidate_id =
                           recurring_payment_candidates.id
                     ORDER BY verified_transactions.transaction_date,
                              verified_transactions.id
                     LIMIT 1),
                    CASE WHEN expected_amount > 0 THEN 'inflow' ELSE 'outflow' END
                ),
                financial_role_id = COALESCE(
                    (SELECT financial_role_audits.new_role_id
                     FROM recurring_payment_members
                     JOIN financial_role_audits
                       ON financial_role_audits.verified_transaction_id =
                          recurring_payment_members.verified_transaction_id
                     WHERE recurring_payment_members.candidate_id =
                           recurring_payment_candidates.id
                       AND financial_role_audits.changed_at <=
                           recurring_payment_candidates.detected_at
                     ORDER BY financial_role_audits.changed_at DESC,
                              financial_role_audits.id DESC
                     LIMIT 1),
                    'unknown'
                ),
                evidence_as_of_date = MIN(
                    COALESCE(
                        (SELECT MAX(verified_transactions.transaction_date)
                         FROM recurring_payment_members
                         JOIN verified_transactions
                           ON verified_transactions.id =
                              recurring_payment_members.verified_transaction_id
                         WHERE recurring_payment_members.candidate_id =
                               recurring_payment_candidates.id),
                        date(detected_at)
                    ),
                    date(detected_at)
                ),
                knowledge_cutoff_at = :legacy_available_at
            """
        ),
        {"legacy_available_at": legacy_available_at},
    )
    op.get_bind().execute(
        sa.text(
            """
            UPDATE recurring_payment_members
            SET identified_at = :legacy_available_at
            """
        ),
        {"legacy_available_at": legacy_available_at},
    )

    with op.batch_alter_table("recurring_payment_candidates", schema=None) as batch_op:
        batch_op.alter_column("currency", existing_type=sa.String(3), nullable=False)
        batch_op.alter_column("direction", existing_type=sa.String(10), nullable=False)
        batch_op.alter_column(
            "financial_role_id", existing_type=sa.String(50), nullable=False
        )
        batch_op.alter_column(
            "evidence_as_of_date", existing_type=sa.Date(), nullable=False
        )
        batch_op.alter_column(
            "knowledge_cutoff_at", existing_type=sa.DateTime(), nullable=False
        )
        batch_op.create_foreign_key(
            "fk_recurring_candidates_financial_role",
            "financial_roles",
            ["financial_role_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_recurring_candidates_currency", "currency = 'GBP'"
        )
        batch_op.create_check_constraint(
            "ck_recurring_candidates_direction",
            "direction IN ('inflow', 'outflow')",
        )
    with op.batch_alter_table("recurring_payment_members", schema=None) as batch_op:
        batch_op.alter_column(
            "identified_at", existing_type=sa.DateTime(), nullable=False
        )


def downgrade() -> None:
    """Remove cutoff provenance while preserving the original candidate fields."""
    with op.batch_alter_table("recurring_payment_members", schema=None) as batch_op:
        batch_op.drop_column("identified_at")
    with op.batch_alter_table("recurring_payment_candidates", schema=None) as batch_op:
        batch_op.drop_constraint("ck_recurring_candidates_direction", type_="check")
        batch_op.drop_constraint("ck_recurring_candidates_currency", type_="check")
        batch_op.drop_constraint(
            "fk_recurring_candidates_financial_role", type_="foreignkey"
        )
        batch_op.drop_column("knowledge_cutoff_at")
        batch_op.drop_column("evidence_as_of_date")
        batch_op.drop_column("financial_role_id")
        batch_op.drop_column("direction")
        batch_op.drop_column("currency")

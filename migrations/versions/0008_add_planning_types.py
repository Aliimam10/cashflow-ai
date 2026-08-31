"""Distinguish persisted budget and financial-goal types.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add explicit planning types while conservatively mapping legacy rows."""
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("budget_type", sa.String(30), nullable=True))
        batch_op.alter_column(
            "category_id",
            existing_type=sa.String(100),
            nullable=True,
        )
    with op.batch_alter_table("savings_goals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("goal_type", sa.String(30), nullable=True))

    connection = op.get_bind()
    connection.execute(sa.text("UPDATE budgets SET budget_type = 'monthly_category'"))
    connection.execute(sa.text("UPDATE savings_goals SET goal_type = 'savings_target'"))

    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.alter_column(
            "budget_type",
            existing_type=sa.String(30),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_budgets_type",
            "budget_type IN ('monthly_category', 'weekly_discretionary')",
        )
        batch_op.create_check_constraint(
            "ck_budgets_shape",
            "(budget_type = 'monthly_category' AND category_id IS NOT NULL) OR "
            "(budget_type = 'weekly_discretionary' AND category_id IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_budgets_period_shape",
            "(budget_type = 'monthly_category' "
            "AND strftime('%d', period_start) = '01' "
            "AND period_end = date(period_start, '+1 month', '-1 day')) OR "
            "(budget_type = 'weekly_discretionary' "
            "AND CAST(strftime('%w', period_start) AS INTEGER) = 1 "
            "AND julianday(period_end) - julianday(period_start) = 6)",
        )
    op.create_index(
        "uq_budgets_weekly_discretionary_period",
        "budgets",
        ["user_profile_id", "period_start", "period_end"],
        unique=True,
        sqlite_where=sa.text("budget_type = 'weekly_discretionary'"),
    )

    with op.batch_alter_table("savings_goals", schema=None) as batch_op:
        batch_op.alter_column(
            "goal_type",
            existing_type=sa.String(30),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_savings_goals_type",
            "goal_type IN ('savings_target', 'minimum_balance')",
        )
        batch_op.create_check_constraint(
            "ck_savings_goals_shape",
            "goal_type = 'savings_target' OR target_date IS NULL",
        )
    op.create_index(
        "uq_savings_goals_minimum_balance",
        "savings_goals",
        ["account_id"],
        unique=True,
        sqlite_where=sa.text("goal_type = 'minimum_balance'"),
    )


def downgrade() -> None:
    """Remove planning types only when no new-only records would be lost."""
    connection = op.get_bind()
    weekly_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM budgets WHERE budget_type = 'weekly_discretionary'"
        )
    )
    minimum_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM savings_goals WHERE goal_type = 'minimum_balance'"
        )
    )
    if weekly_count or minimum_count:
        raise RuntimeError(
            "cannot downgrade planning types while weekly budgets or "
            "minimum-balance goals exist"
        )

    op.drop_index(
        "uq_savings_goals_minimum_balance",
        table_name="savings_goals",
    )
    with op.batch_alter_table("savings_goals", schema=None) as batch_op:
        batch_op.drop_constraint("ck_savings_goals_shape", type_="check")
        batch_op.drop_constraint("ck_savings_goals_type", type_="check")
        batch_op.drop_column("goal_type")

    op.drop_index(
        "uq_budgets_weekly_discretionary_period",
        table_name="budgets",
    )
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.drop_constraint("ck_budgets_period_shape", type_="check")
        batch_op.drop_constraint("ck_budgets_shape", type_="check")
        batch_op.drop_constraint("ck_budgets_type", type_="check")
        batch_op.alter_column(
            "category_id",
            existing_type=sa.String(100),
            nullable=False,
        )
        batch_op.drop_column("budget_type")

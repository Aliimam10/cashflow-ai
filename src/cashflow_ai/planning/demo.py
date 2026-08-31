"""Human-readable synthetic demonstration of budgets, goals, and safe spending."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    CategoryRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.planning.service import (
    create_budget,
    create_financial_goal,
    evaluate_financial_plan,
)
from cashflow_ai.schemas.planning import (
    BudgetCreate,
    BudgetType,
    FinancialGoalCreate,
    FinancialGoalType,
    PlanningBalanceProjection,
    PlanningEvaluationPlan,
)
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import Currency, FinancialRole

_AS_OF = date(2026, 8, 14)
_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_database(*, incomplete_coverage: bool) -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id="synthetic-account",
                user_profile_id="synthetic-profile",
                name="Fictional Current Account",
                account_type="current",
                currency="GBP",
            )
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add(
            CategoryRecord(
                id="food",
                name="Food",
                taxonomy_version="1.0",
                is_active=True,
            )
        )
        batch = ImportBatchRecord(
            id="synthetic-batch",
            account_id="synthetic-account",
            source_type="csv",
            source_filename="fictional.csv",
            file_hash=_digest("fictional-file"),
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=_NOW,
        )
        session.add(batch)
        context = ImportContextRecord(
            id="synthetic-context",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=_NOW,
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id="synthetic-coverage",
                import_context_id=context.id,
                statement_start_date=date(2026, 8, 1),
                statement_end_date=(
                    date(2026, 8, 12) if incomplete_coverage else _AS_OF
                ),
                coverage_status="complete",
                missing_periods_json=[],
            )
        )
        for row_number, (identifier, transaction_date, amount) in enumerate(
            (
                ("fictional-early", date(2026, 8, 3), Decimal("-70.00")),
                ("fictional-recent", date(2026, 8, 11), Decimal("-30.00")),
            ),
            start=2,
        ):
            raw = RawTransactionRecord(
                id=f"raw-{identifier}",
                import_batch_id=batch.id,
                source_type="csv",
                source_row_number=row_number,
                page_number=None,
                page_record_number=None,
                raw_payload={"synthetic": True},
                original_date_text=transaction_date.isoformat(),
                original_description="Fictional purchase",
                original_amount_text=str(amount),
                parser_name="synthetic_parser",
                parser_version="1.0",
                source_fingerprint=_digest(f"source-{identifier}"),
                canonical_fingerprint=_digest(f"canonical-{identifier}"),
                issues_json=[],
                review_status="confirmed",
                created_at=_NOW,
            )
            session.add(raw)
            session.add(
                VerifiedTransactionRecord(
                    id=identifier,
                    raw_transaction_id=raw.id,
                    account_id="synthetic-account",
                    transaction_date=transaction_date,
                    posting_date=None,
                    description="Fictional purchase",
                    merchant="Fictional Merchant",
                    amount=amount,
                    balance_after=None,
                    currency="GBP",
                    external_id=identifier,
                    transaction_type="synthetic",
                    direction="outflow",
                    category_id="food",
                    financial_role_id=FinancialRole.EXPENSE.value,
                    verified_at=_NOW,
                )
            )
    return factory


def _create_plan(factory: sessionmaker[Session]) -> None:
    profile = "synthetic-profile"
    account = "synthetic-account"
    create_budget(
        factory,
        request=BudgetCreate(
            user_profile_id=profile,
            budget_type=BudgetType.MONTHLY_CATEGORY,
            category_id="food",
            period=DateRange(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            ),
            amount_limit=Decimal("200.00"),
        ),
    )
    create_budget(
        factory,
        request=BudgetCreate(
            user_profile_id=profile,
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=DateRange(
                start_date=date(2026, 8, 10),
                end_date=date(2026, 8, 16),
            ),
            amount_limit=Decimal("40.00"),
        ),
    )
    create_budget(
        factory,
        request=BudgetCreate(
            user_profile_id=profile,
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=DateRange(
                start_date=date(2026, 8, 17),
                end_date=date(2026, 8, 23),
            ),
            amount_limit=Decimal("60.00"),
        ),
    )
    create_financial_goal(
        factory,
        request=FinancialGoalCreate(
            user_profile_id=profile,
            account_id=account,
            goal_type=FinancialGoalType.SAVINGS_TARGET,
            name="Fictional savings target",
            target_amount=Decimal("1000.00"),
            current_amount=Decimal("400.00"),
            target_date=date(2026, 12, 31),
            as_of_date=_AS_OF,
        ),
    )
    create_financial_goal(
        factory,
        request=FinancialGoalCreate(
            user_profile_id=profile,
            account_id=account,
            goal_type=FinancialGoalType.MINIMUM_BALANCE,
            name="Fictional balance floor",
            target_amount=Decimal("900.00"),
            current_amount=Decimal("0.00"),
            target_date=None,
            as_of_date=_AS_OF,
        ),
    )


def main() -> None:
    """Print deterministic planning results using fictional local-only data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incomplete-coverage", action="store_true")
    parser.add_argument("--forecast-low-balance", type=Decimal, default=Decimal("800"))
    args = parser.parse_args()
    factory = _seed_database(incomplete_coverage=args.incomplete_coverage)
    _create_plan(factory)
    result = evaluate_financial_plan(
        factory,
        plan=PlanningEvaluationPlan(
            user_profile_id="synthetic-profile",
            account_ids=("synthetic-account",),
            as_of_date=_AS_OF,
        ),
        balance_projections=(
            PlanningBalanceProjection(
                account_id="synthetic-account",
                currency=Currency.GBP,
                period=DateRange(
                    start_date=date(2026, 8, 17),
                    end_date=date(2026, 9, 13),
                ),
                lowest_lower_balance=args.forecast_low_balance,
                expected_end_balance=Decimal("900.00"),
                lower_end_balance=max(args.forecast_low_balance, Decimal("850.00")),
                expected_discretionary_spending=Decimal("400.00"),
            ),
        ),
    )
    category = next(
        item
        for item in result.budgets
        if item.budget.budget_type is BudgetType.MONTHLY_CATEGORY
    )
    savings = next(
        item
        for item in result.goals
        if item.goal.goal_type is FinancialGoalType.SAVINGS_TARGET
    )
    print("CashFlow AI synthetic financial-planning check")
    print(
        "transaction coverage: "
        f"{category.observation_period.start_date.isoformat()} to "
        f"{category.observation_period.end_date.isoformat()} "
        f"({category.coverage.status.value})"
    )
    print(f"food budget used: GBP {category.amount_used}")
    print(f"food projected month use: {category.projected_use or 'unavailable'}")
    print(
        "required monthly savings contribution: "
        f"GBP {savings.required_monthly_contribution}"
    )
    safe_amount = result.safe_spending.safe_weekly_spending
    print(f"safe weekly spending estimate: GBP {safe_amount}")
    print(
        "warnings: "
        + (
            ", ".join(item.code.value for item in result.warnings)
            if result.warnings
            else "none"
        )
    )
    print("financial advice guarantee: false")


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()

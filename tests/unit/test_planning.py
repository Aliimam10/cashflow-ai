"""Tests for coverage-aware budgets, goals, and safe-spending estimates."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    BudgetRecord,
    CategoryRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    SavingsGoalRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import AccountRepository, PlanningRepository
from cashflow_ai.planning import (
    PlanningServiceError,
    PlanningServiceErrorCode,
    create_budget,
    create_financial_goal,
    evaluate_financial_plan,
    list_budgets,
    list_financial_goals,
)
from cashflow_ai.planning.demo import main as demo_main
from cashflow_ai.schemas import (
    AnalyticsCoverageStatus,
    Budget,
    BudgetCreate,
    BudgetProgress,
    BudgetType,
    Currency,
    DataCoverageIndicator,
    DateRange,
    FinancialGoal,
    FinancialGoalCreate,
    FinancialGoalProgress,
    FinancialGoalType,
    FinancialPlanningResult,
    ForecastPathWarningCode,
    PlanningBalanceProjection,
    PlanningEvaluationPlan,
    PlanningWarning,
    PlanningWarningCode,
    SafeSpendingLimitingFactor,
    SafeWeeklySpending,
)
from cashflow_ai.schemas.analytics import AccountCoverageIndicator
from cashflow_ai.schemas.transactions import FinancialRole

AS_OF = date(2026, 8, 14)
NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
MONTH = DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
CURRENT_WEEK = DateRange(start_date=date(2026, 8, 10), end_date=date(2026, 8, 16))
FORECAST_WEEK = DateRange(start_date=date(2026, 8, 17), end_date=date(2026, 8, 23))
FORECAST_PERIOD = DateRange(
    start_date=FORECAST_WEEK.start_date,
    end_date=date(2026, 9, 13),
)


@pytest.fixture
def engine() -> Engine:
    value = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(value)
    return value


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    value = create_session_factory(engine)
    _seed_foundation(value)
    return value


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_foundation(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.add_all(
            [
                UserProfileRecord(
                    id="synthetic-profile",
                    display_name="Fictional User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
                UserProfileRecord(
                    id="other-profile",
                    display_name="Other Fictional User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="synthetic-account",
                    user_profile_id="synthetic-profile",
                    name="Fictional Current Account",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id="inactive-account",
                    user_profile_id="synthetic-profile",
                    name="Inactive Fictional Account",
                    account_type="current",
                    currency="GBP",
                    is_active=False,
                ),
                AccountRecord(
                    id="other-account",
                    user_profile_id="other-profile",
                    name="Other Fictional Account",
                    account_type="current",
                    currency="GBP",
                ),
            ]
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            [
                CategoryRecord(
                    id="food",
                    name="Food",
                    taxonomy_version="1.0",
                    is_active=True,
                ),
                CategoryRecord(
                    id="old_category",
                    name="Old Category",
                    taxonomy_version="1.0",
                    is_active=False,
                ),
            ]
        )


def _add_coverage(
    factory: sessionmaker[Session],
    *,
    end_date: date = AS_OF,
) -> None:
    with session_scope(factory) as session:
        batch = ImportBatchRecord(
            id="synthetic-batch",
            account_id="synthetic-account",
            source_type="csv",
            source_filename="fictional.csv",
            file_hash=_digest("fictional-file"),
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=NOW,
        )
        session.add(batch)
        context = ImportContextRecord(
            id="synthetic-context",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=NOW,
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id="synthetic-coverage",
                import_context_id=context.id,
                statement_start_date=MONTH.start_date,
                statement_end_date=end_date,
                coverage_status="complete",
                missing_periods_json=[],
            )
        )


def _add_expense(
    factory: sessionmaker[Session],
    identifier: str,
    *,
    transaction_date: date,
    amount: str,
) -> None:
    with session_scope(factory) as session:
        raw = RawTransactionRecord(
            id=f"raw-{identifier}",
            import_batch_id="synthetic-batch",
            source_type="csv",
            source_row_number=2,
            page_number=None,
            page_record_number=None,
            raw_payload={"synthetic": True},
            original_date_text=transaction_date.isoformat(),
            original_description="Fictional purchase",
            original_amount_text=amount,
            parser_name="synthetic_parser",
            parser_version="1.0",
            source_fingerprint=_digest(f"source-{identifier}"),
            canonical_fingerprint=_digest(f"canonical-{identifier}"),
            issues_json=[],
            review_status="confirmed",
            created_at=NOW,
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
                amount=Decimal(amount),
                balance_after=None,
                currency="GBP",
                external_id=identifier,
                transaction_type="synthetic",
                direction="outflow",
                category_id="food",
                financial_role_id=FinancialRole.EXPENSE.value,
                verified_at=NOW,
            )
        )


def _budget_request(
    *,
    budget_type: BudgetType = BudgetType.MONTHLY_CATEGORY,
    category_id: str | None = "food",
    period: DateRange = MONTH,
    amount: str = "200.00",
    profile_id: str = "synthetic-profile",
) -> BudgetCreate:
    return BudgetCreate(
        user_profile_id=profile_id,
        budget_type=budget_type,
        category_id=category_id,
        period=period,
        amount_limit=Decimal(amount),
    )


def _goal_request(
    *,
    goal_type: FinancialGoalType = FinancialGoalType.SAVINGS_TARGET,
    name: str = "Fictional savings target",
    target: str = "1000.00",
    current: str = "400.00",
    target_date: date | None = date(2026, 12, 31),
    profile_id: str = "synthetic-profile",
    account_id: str = "synthetic-account",
) -> FinancialGoalCreate:
    return FinancialGoalCreate(
        user_profile_id=profile_id,
        account_id=account_id,
        goal_type=goal_type,
        name=name,
        target_amount=Decimal(target),
        current_amount=Decimal(current),
        target_date=target_date,
        as_of_date=AS_OF,
    )


def _projection(
    *,
    account_id: str = "synthetic-account",
    period: DateRange = FORECAST_PERIOD,
    lowest: str = "800.00",
    lower_end: str = "850.00",
    spending: str = "400.00",
    warnings: tuple[ForecastPathWarningCode, ...] = (
        ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,
    ),
) -> PlanningBalanceProjection:
    return PlanningBalanceProjection(
        account_id=account_id,
        currency=Currency.GBP,
        period=period,
        lowest_lower_balance=Decimal(lowest),
        expected_end_balance=Decimal("900.00"),
        lower_end_balance=Decimal(lower_end),
        expected_discretionary_spending=Decimal(spending),
        forecast_warnings=warnings,
    )


def _plan(
    *, account_ids: tuple[str, ...] = ("synthetic-account",)
) -> PlanningEvaluationPlan:
    return PlanningEvaluationPlan(
        user_profile_id="synthetic-profile",
        account_ids=account_ids,
        as_of_date=AS_OF,
    )


def _create_complete_plan(factory: sessionmaker[Session]) -> None:
    _add_coverage(factory)
    _add_expense(
        factory,
        "expense-early",
        transaction_date=date(2026, 8, 3),
        amount="-70.00",
    )
    _add_expense(
        factory,
        "expense-current-week",
        transaction_date=date(2026, 8, 11),
        amount="-30.00",
    )
    create_budget(factory, request=_budget_request())
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=CURRENT_WEEK,
            amount="40.00",
        ),
    )
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=FORECAST_WEEK,
            amount="60.00",
        ),
    )
    create_financial_goal(factory, request=_goal_request())
    create_financial_goal(
        factory,
        request=_goal_request(
            goal_type=FinancialGoalType.MINIMUM_BALANCE,
            name="Fictional balance floor",
            target="900.00",
            current="0.00",
            target_date=None,
        ),
    )


def test_create_budgets_and_goals_persists_explicit_types(
    factory: sessionmaker[Session],
) -> None:
    category = create_budget(factory, request=_budget_request())
    weekly = create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=CURRENT_WEEK,
            amount="50.00",
        ),
    )
    savings = create_financial_goal(factory, request=_goal_request())
    floor = create_financial_goal(
        factory,
        request=_goal_request(
            goal_type=FinancialGoalType.MINIMUM_BALANCE,
            name="Fictional floor",
            target="250.00",
            current="0.00",
            target_date=None,
        ),
    )

    assert category.category_id == "food"
    assert category.budget_type is BudgetType.MONTHLY_CATEGORY
    assert weekly.category_id is None
    assert weekly.budget_type is BudgetType.WEEKLY_DISCRETIONARY
    assert savings.goal_type is FinancialGoalType.SAVINGS_TARGET
    assert savings.created_at.tzinfo is UTC
    assert floor.goal_type is FinancialGoalType.MINIMUM_BALANCE
    with session_scope(factory) as session:
        assert session.scalar(
            select(BudgetRecord).where(BudgetRecord.id == weekly.budget_id)
        )
        assert session.scalar(
            select(SavingsGoalRecord).where(SavingsGoalRecord.id == floor.goal_id)
        )


def test_list_budgets_and_goals_enforces_profile_scope(
    factory: sessionmaker[Session],
) -> None:
    budget = create_budget(factory, request=_budget_request())
    goal = create_financial_goal(factory, request=_goal_request())

    assert list_budgets(
        factory,
        user_profile_id="synthetic-profile",
        as_of_date=AS_OF,
    ) == (budget,)
    assert list_financial_goals(factory, user_profile_id="synthetic-profile") == (goal,)

    with pytest.raises(PlanningServiceError) as budget_error:
        list_budgets(
            factory,
            user_profile_id="missing-profile",
            as_of_date=AS_OF,
        )
    assert budget_error.value.code is PlanningServiceErrorCode.PROFILE_NOT_FOUND
    with pytest.raises(PlanningServiceError) as goal_error:
        list_financial_goals(factory, user_profile_id="missing-profile")
    assert goal_error.value.code is PlanningServiceErrorCode.PROFILE_NOT_FOUND


def test_create_budget_rejects_missing_profile_category_and_duplicates(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(PlanningServiceError) as profile_error:
        create_budget(
            factory,
            request=_budget_request(profile_id="missing-profile"),
        )
    assert profile_error.value.code is PlanningServiceErrorCode.PROFILE_NOT_FOUND

    with pytest.raises(PlanningServiceError) as category_error:
        create_budget(
            factory,
            request=_budget_request(category_id="missing-category"),
        )
    assert category_error.value.code is PlanningServiceErrorCode.CATEGORY_NOT_FOUND

    with pytest.raises(PlanningServiceError) as inactive_error:
        create_budget(
            factory,
            request=_budget_request(category_id="old_category"),
        )
    assert inactive_error.value.code is PlanningServiceErrorCode.CATEGORY_INACTIVE

    create_budget(factory, request=_budget_request())
    with pytest.raises(PlanningServiceError) as duplicate_error:
        create_budget(factory, request=_budget_request())
    assert duplicate_error.value.code is PlanningServiceErrorCode.DUPLICATE_BUDGET


def test_create_goal_rejects_invalid_ownership_state_currency_and_duplicates(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PlanningServiceError) as profile_error:
        create_financial_goal(
            factory,
            request=_goal_request(profile_id="missing-profile"),
        )
    assert profile_error.value.code is PlanningServiceErrorCode.PROFILE_NOT_FOUND

    for account_id in ("missing-account", "other-account"):
        with pytest.raises(PlanningServiceError) as account_error:
            create_financial_goal(
                factory,
                request=_goal_request(account_id=account_id),
            )
        assert account_error.value.code is PlanningServiceErrorCode.ACCOUNT_NOT_FOUND

    with pytest.raises(PlanningServiceError) as inactive_error:
        create_financial_goal(
            factory,
            request=_goal_request(account_id="inactive-account"),
        )
    assert inactive_error.value.code is PlanningServiceErrorCode.ACCOUNT_INACTIVE

    monkeypatch.setattr(
        AccountRepository,
        "get",
        lambda repository, account_id: SimpleNamespace(
            id=account_id,
            user_profile_id="synthetic-profile",
            is_active=True,
            currency="USD",
        ),
    )
    with pytest.raises(PlanningServiceError) as currency_error:
        create_financial_goal(factory, request=_goal_request())
    assert currency_error.value.code is PlanningServiceErrorCode.CURRENCY_MISMATCH
    monkeypatch.undo()

    create_financial_goal(factory, request=_goal_request())
    with pytest.raises(PlanningServiceError) as duplicate_error:
        create_financial_goal(factory, request=_goal_request())
    assert duplicate_error.value.code is PlanningServiceErrorCode.DUPLICATE_GOAL

    create_financial_goal(
        factory,
        request=_goal_request(
            goal_type=FinancialGoalType.MINIMUM_BALANCE,
            name="First floor",
            target="100.00",
            current="0.00",
            target_date=None,
        ),
    )
    with pytest.raises(PlanningServiceError) as floor_error:
        create_financial_goal(
            factory,
            request=_goal_request(
                goal_type=FinancialGoalType.MINIMUM_BALANCE,
                name="Second floor",
                target="200.00",
                current="0.00",
                target_date=None,
            ),
        )
    assert floor_error.value.code is PlanningServiceErrorCode.DUPLICATE_GOAL


def test_evaluate_plan_calculates_progress_contributions_and_safe_spending(
    factory: sessionmaker[Session],
) -> None:
    _create_complete_plan(factory)

    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(_projection(),),
    )

    progress = {item.budget.budget_type: item for item in result.budgets}
    category = progress[BudgetType.MONTHLY_CATEGORY]
    weekly = progress[BudgetType.WEEKLY_DISCRETIONARY]
    assert category.observation_period == DateRange(
        start_date=MONTH.start_date,
        end_date=AS_OF,
    )
    assert category.coverage.status is AnalyticsCoverageStatus.COMPLETE
    assert category.amount_used == Decimal("100.00")
    assert category.amount_remaining == Decimal("100.00")
    assert category.projected_use == Decimal("221.43")
    assert category.projected_overrun == Decimal("21.43")
    assert weekly.amount_used == Decimal("30.00")
    assert weekly.projected_use == Decimal("42.00")
    assert weekly.projected_overrun == Decimal("2.00")

    goals = {item.goal.goal_type: item for item in result.goals}
    savings = goals[FinancialGoalType.SAVINGS_TARGET]
    floor = goals[FinancialGoalType.MINIMUM_BALANCE]
    assert savings.remaining_amount == Decimal("600.00")
    assert savings.contribution_months == 5
    assert savings.required_monthly_contribution == Decimal("120.00")
    assert savings.projected_shortfall is None
    assert floor.forecast_lowest_balance == Decimal("800.00")
    assert floor.projected_shortfall == Decimal("100.00")

    safe = result.safe_spending
    assert safe.forecast_weeks == Decimal("4")
    assert safe.expected_forecast_weekly_spending == Decimal("100.00")
    assert safe.lower_balance_headroom == Decimal("-100.00")
    assert safe.required_weekly_savings == Decimal("27.70")
    assert safe.cash_based_weekly_limit == Decimal("47.30")
    assert safe.weekly_budget_limit == Decimal("60.00")
    assert safe.safe_weekly_spending == Decimal("47.30")
    assert safe.limiting_factor is SafeSpendingLimitingFactor.CASH_HEADROOM
    assert {item.code for item in result.warnings} == {
        PlanningWarningCode.PROJECTED_CATEGORY_BUDGET_SHORTFALL,
        PlanningWarningCode.PROJECTED_WEEKLY_BUDGET_SHORTFALL,
        PlanningWarningCode.MINIMUM_BALANCE_SHORTFALL,
        PlanningWarningCode.FORECAST_LIMITATION,
    }


def test_incomplete_coverage_withholds_budget_projections(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory, end_date=date(2026, 8, 12))
    _add_expense(
        factory,
        "expense-observed",
        transaction_date=date(2026, 8, 11),
        amount="-30.00",
    )
    create_budget(factory, request=_budget_request())
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=CURRENT_WEEK,
            amount="40.00",
        ),
    )

    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(_projection(warnings=()),),
    )

    assert all(
        item.coverage.status is AnalyticsCoverageStatus.PARTIAL
        for item in result.budgets
    )
    assert all(item.amount_used == Decimal("30.00") for item in result.budgets)
    assert all(item.projected_use is None for item in result.budgets)
    assert all(item.projected_overrun is None for item in result.budgets)
    assert [item.code for item in result.warnings] == [
        PlanningWarningCode.INCOMPLETE_TRANSACTION_COVERAGE,
        PlanningWarningCode.INCOMPLETE_TRANSACTION_COVERAGE,
    ]


def test_missing_coverage_keeps_observed_budget_amounts_unavailable(
    factory: sessionmaker[Session],
) -> None:
    create_budget(factory, request=_budget_request())
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=CURRENT_WEEK,
            amount="40.00",
        ),
    )

    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(_projection(warnings=()),),
    )

    assert all(
        item.coverage.status is AnalyticsCoverageStatus.MISSING
        for item in result.budgets
    )
    assert all(item.amount_used is None for item in result.budgets)
    assert all(item.amount_remaining is None for item in result.budgets)
    assert all(item.projected_use is None for item in result.budgets)


def test_complete_budget_with_no_projected_overrun_adds_no_budget_warning(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory)
    _add_expense(
        factory,
        "small-expense",
        transaction_date=date(2026, 8, 3),
        amount="-10.00",
    )
    create_budget(factory, request=_budget_request(amount="1000.00"))

    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(_projection(warnings=()),),
    )

    assert result.budgets[0].projected_overrun == Decimal("0.00")
    assert result.warnings == ()


def test_overdue_and_legacy_savings_targets_return_explicit_warnings(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        session.add_all(
            [
                SavingsGoalRecord(
                    id="overdue-goal",
                    account_id="synthetic-account",
                    goal_type="savings_target",
                    name="Overdue fictional goal",
                    target_amount=Decimal("100.00"),
                    current_amount=Decimal("20.00"),
                    target_date=date(2026, 7, 1),
                    created_at=NOW,
                ),
                SavingsGoalRecord(
                    id="legacy-goal",
                    account_id="synthetic-account",
                    goal_type="savings_target",
                    name="Legacy fictional goal",
                    target_amount=Decimal("50.00"),
                    current_amount=Decimal("10.00"),
                    target_date=None,
                    created_at=NOW,
                ),
            ]
        )

    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(
            _projection(lowest="0.00", lower_end="0.00", spending="0.00", warnings=()),
        ),
    )

    assert {item.required_monthly_contribution for item in result.goals} == {
        Decimal("40.00"),
        Decimal("80.00"),
    }
    assert {item.code for item in result.warnings} >= {
        PlanningWarningCode.OVERDUE_SAVINGS_TARGET,
        PlanningWarningCode.MISSING_SAVINGS_TARGET_DATE,
        PlanningWarningCode.SAVINGS_CONTRIBUTION_SHORTFALL,
    }
    assert result.safe_spending.safe_weekly_spending == Decimal("0.00")
    assert (
        result.safe_spending.limiting_factor is SafeSpendingLimitingFactor.NO_HEADROOM
    )


def test_weekly_budget_can_be_the_limiting_factor(
    factory: sessionmaker[Session],
) -> None:
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=FORECAST_WEEK,
            amount="60.00",
        ),
    )
    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(
            _projection(lowest="1000.00", lower_end="1000.00", warnings=()),
        ),
    )
    assert result.safe_spending.cash_based_weekly_limit == Decimal("350.00")
    assert result.safe_spending.safe_weekly_spending == Decimal("60.00")
    assert (
        result.safe_spending.limiting_factor is SafeSpendingLimitingFactor.WEEKLY_BUDGET
    )


def test_equal_cash_and_budget_limits_report_both_constraints(
    factory: sessionmaker[Session],
) -> None:
    create_budget(
        factory,
        request=_budget_request(
            budget_type=BudgetType.WEEKLY_DISCRETIONARY,
            category_id=None,
            period=FORECAST_WEEK,
            amount="100.00",
        ),
    )
    result = evaluate_financial_plan(
        factory,
        plan=_plan(),
        balance_projections=(
            _projection(
                lowest="0.00",
                lower_end="0.00",
                spending="400.00",
                warnings=(),
            ),
        ),
    )
    assert result.safe_spending.cash_based_weekly_limit == Decimal("100.00")
    assert (
        result.safe_spending.limiting_factor
        is SafeSpendingLimitingFactor.CASH_AND_BUDGET
    )


def test_evaluation_rejects_projection_account_period_and_account_state(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(PlanningServiceError) as scope_error:
        evaluate_financial_plan(
            factory,
            plan=_plan(),
            balance_projections=(_projection(account_id="other-account"),),
        )
    assert scope_error.value.code is PlanningServiceErrorCode.PROJECTION_SCOPE_MISMATCH

    with pytest.raises(PlanningServiceError) as period_error:
        evaluate_financial_plan(
            factory,
            plan=_plan(),
            balance_projections=(
                _projection(
                    period=DateRange(
                        start_date=date(2026, 8, 10),
                        end_date=date(2026, 9, 6),
                    )
                ),
            ),
        )
    assert (
        period_error.value.code is PlanningServiceErrorCode.PROJECTION_PERIOD_MISMATCH
    )

    with pytest.raises(PlanningServiceError) as account_error:
        evaluate_financial_plan(
            factory,
            plan=_plan(account_ids=("other-account",)),
            balance_projections=(_projection(account_id="other-account"),),
        )
    assert account_error.value.code is PlanningServiceErrorCode.ACCOUNT_NOT_FOUND

    with pytest.raises(PlanningServiceError) as inactive_error:
        evaluate_financial_plan(
            factory,
            plan=_plan(account_ids=("inactive-account",)),
            balance_projections=(_projection(account_id="inactive-account"),),
        )
    assert inactive_error.value.code is PlanningServiceErrorCode.ACCOUNT_INACTIVE


def test_evaluation_rejects_corrupt_currency_and_cross_profile_goal_rows(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as context:
        context.setattr(
            AccountRepository,
            "get",
            lambda repository, account_id: SimpleNamespace(
                id=account_id,
                user_profile_id="synthetic-profile",
                is_active=True,
                currency="USD",
            ),
        )
        with pytest.raises(PlanningServiceError) as account_currency_error:
            evaluate_financial_plan(
                factory,
                plan=_plan(),
                balance_projections=(_projection(),),
            )
    assert (
        account_currency_error.value.code is PlanningServiceErrorCode.CURRENCY_MISMATCH
    )

    corrupt_projection = _projection().model_copy(
        update={"currency": SimpleNamespace(value="USD")}
    )
    with pytest.raises(PlanningServiceError) as projection_currency_error:
        evaluate_financial_plan(
            factory,
            plan=_plan(),
            balance_projections=(corrupt_projection,),
        )
    assert (
        projection_currency_error.value.code
        is PlanningServiceErrorCode.CURRENCY_MISMATCH
    )

    with monkeypatch.context() as context:
        context.setattr(
            PlanningRepository,
            "list_goals_for_accounts",
            lambda repository, account_ids: (
                (
                    SimpleNamespace(),
                    SimpleNamespace(user_profile_id="other-profile"),
                ),
            ),
        )
        with pytest.raises(PlanningServiceError) as ownership_error:
            evaluate_financial_plan(
                factory,
                plan=_plan(),
                balance_projections=(_projection(),),
            )
    assert ownership_error.value.code is PlanningServiceErrorCode.ACCOUNT_NOT_FOUND


def _coverage(status: AnalyticsCoverageStatus) -> DataCoverageIndicator:
    requested = DateRange(start_date=date(2026, 8, 1), end_date=AS_OF)
    complete = status is AnalyticsCoverageStatus.COMPLETE
    missing = status is AnalyticsCoverageStatus.MISSING
    return DataCoverageIndicator(
        requested_period=requested,
        status=status,
        fully_covered_periods=(requested,) if complete else (),
        partially_covered_periods=(),
        missing_periods=(requested,) if missing else (),
        requested_days=14,
        fully_covered_days=14 if complete else 0,
        partially_covered_days=0,
        missing_days=14 if missing else 0,
        accounts=(
            AccountCoverageIndicator(
                account_id="synthetic-account",
                status=status,
                covered_periods=(requested,) if complete else (),
                missing_periods=(requested,) if missing else (),
                covered_days=14 if complete else 0,
                missing_days=14 if missing else 0,
            ),
        ),
    )


def _stored_budget() -> Budget:
    return Budget(
        budget_id="budget-1",
        user_profile_id="synthetic-profile",
        budget_type=BudgetType.MONTHLY_CATEGORY,
        category_id="food",
        period=MONTH,
        amount_limit=Decimal("100.00"),
        currency=Currency.GBP,
    )


def _stored_goal(goal_type: FinancialGoalType) -> FinancialGoal:
    return FinancialGoal(
        goal_id="goal-1",
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        goal_type=goal_type,
        name="Fictional goal",
        target_amount=Decimal("100.00"),
        current_amount=(
            Decimal("0.00")
            if goal_type is FinancialGoalType.MINIMUM_BALANCE
            else Decimal("10.00")
        ),
        target_date=(
            None
            if goal_type is FinancialGoalType.MINIMUM_BALANCE
            else date(2026, 12, 31)
        ),
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"category_id": None}, "requires a category and full month"),
        (
            {"period": DateRange(start_date=date(2026, 8, 2), end_date=MONTH.end_date)},
            "requires a category and full month",
        ),
        (
            {
                "budget_type": BudgetType.WEEKLY_DISCRETIONARY,
                "category_id": "food",
                "period": CURRENT_WEEK,
            },
            "Monday through Sunday",
        ),
        (
            {
                "budget_type": BudgetType.WEEKLY_DISCRETIONARY,
                "category_id": None,
                "period": DateRange(
                    start_date=CURRENT_WEEK.start_date + timedelta(days=1),
                    end_date=CURRENT_WEEK.end_date,
                ),
            },
            "Monday through Sunday",
        ),
    ],
)
def test_budget_contract_rejects_invalid_shapes(
    values: dict[str, Any], message: str
) -> None:
    base = _budget_request().model_dump()
    base.update(values)
    with pytest.raises(ValidationError, match=message):
        BudgetCreate(**base)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"target_date": None}, "requires a future target date"),
        ({"target_date": AS_OF}, "requires a future target date"),
        (
            {
                "goal_type": FinancialGoalType.MINIMUM_BALANCE,
                "target_date": date(2026, 12, 31),
                "current_amount": Decimal("0.00"),
            },
            "cannot contain a date or saved amount",
        ),
        (
            {
                "goal_type": FinancialGoalType.MINIMUM_BALANCE,
                "target_date": None,
                "current_amount": Decimal("1.00"),
            },
            "cannot contain a date or saved amount",
        ),
    ],
)
def test_goal_create_contract_rejects_invalid_shapes(
    values: dict[str, Any], message: str
) -> None:
    base = _goal_request().model_dump()
    base.update(values)
    with pytest.raises(ValidationError, match=message):
        FinancialGoalCreate(**base)


def test_planning_contracts_reject_inconsistent_derived_state() -> None:
    with pytest.raises(ValidationError, match="account IDs must be unique"):
        _plan(account_ids=("synthetic-account", "synthetic-account"))
    with pytest.raises(ValidationError, match="must start on Monday"):
        _projection(
            period=DateRange(
                start_date=FORECAST_PERIOD.start_date + timedelta(days=1),
                end_date=FORECAST_PERIOD.end_date,
            )
        )
    with pytest.raises(ValidationError, match="exceeds the final lower"):
        _projection(lowest="900.00", lower_end="850.00")
    with pytest.raises(ValidationError, match="cannot contain a date"):
        FinancialGoal(
            **_stored_goal(FinancialGoalType.MINIMUM_BALANCE).model_dump(
                exclude={"target_date"}
            ),
            target_date=date(2026, 12, 31),
        )

    budget = _stored_budget()
    coverage = _coverage(AnalyticsCoverageStatus.COMPLETE)
    with pytest.raises(ValidationError, match="coverage must describe"):
        BudgetProgress(
            budget=budget,
            observation_period=DateRange(start_date=date(2026, 8, 2), end_date=AS_OF),
            coverage=coverage,
            amount_used=Decimal("10.00"),
            amount_remaining=Decimal("90.00"),
            projected_use=Decimal("20.00"),
            projected_overrun=Decimal("0.00"),
        )
    with pytest.raises(ValidationError, match="cannot claim derived"):
        BudgetProgress(
            budget=budget,
            observation_period=coverage.requested_period,
            coverage=coverage,
            amount_used=None,
            amount_remaining=Decimal("90.00"),
            projected_use=None,
            projected_overrun=None,
        )
    with pytest.raises(ValidationError, match="remaining amount is inconsistent"):
        BudgetProgress(
            budget=budget,
            observation_period=coverage.requested_period,
            coverage=coverage,
            amount_used=Decimal("10.00"),
            amount_remaining=Decimal("80.00"),
            projected_use=Decimal("20.00"),
            projected_overrun=Decimal("0.00"),
        )
    with pytest.raises(ValidationError, match="availability must follow coverage"):
        BudgetProgress(
            budget=budget,
            observation_period=coverage.requested_period,
            coverage=coverage,
            amount_used=Decimal("10.00"),
            amount_remaining=Decimal("90.00"),
            projected_use=None,
            projected_overrun=None,
        )
    missing = _coverage(AnalyticsCoverageStatus.MISSING)
    with pytest.raises(ValidationError, match="cannot claim an overrun"):
        BudgetProgress(
            budget=budget,
            observation_period=missing.requested_period,
            coverage=missing,
            amount_used=Decimal("10.00"),
            amount_remaining=Decimal("90.00"),
            projected_use=None,
            projected_overrun=Decimal("0.00"),
        )
    with pytest.raises(ValidationError, match="overrun is inconsistent"):
        BudgetProgress(
            budget=budget,
            observation_period=coverage.requested_period,
            coverage=coverage,
            amount_used=Decimal("10.00"),
            amount_remaining=Decimal("90.00"),
            projected_use=Decimal("120.00"),
            projected_overrun=Decimal("10.00"),
        )


def test_goal_safe_spending_and_result_contracts_reject_misalignment() -> None:
    savings = _stored_goal(FinancialGoalType.SAVINGS_TARGET)
    floor = _stored_goal(FinancialGoalType.MINIMUM_BALANCE)
    with pytest.raises(ValidationError, match="fields do not match"):
        FinancialGoalProgress(
            goal=savings,
            remaining_amount=Decimal("90.00"),
            forecast_lowest_balance=Decimal("80.00"),
        )
    with pytest.raises(ValidationError, match="forecast shortfall"):
        FinancialGoalProgress(
            goal=savings,
            remaining_amount=Decimal("90.00"),
            contribution_months=5,
            required_monthly_contribution=Decimal("18.00"),
            projected_shortfall=Decimal("0.00"),
        )
    valid_floor_progress = FinancialGoalProgress(
        goal=floor,
        remaining_amount=Decimal("0.00"),
        forecast_lowest_balance=Decimal("80.00"),
        projected_shortfall=Decimal("20.00"),
    )
    with pytest.raises(ValidationError, match="does not match its constraints"):
        SafeWeeklySpending(
            currency=Currency.GBP,
            projection_period=FORECAST_PERIOD,
            forecast_weeks=Decimal("4"),
            expected_forecast_weekly_spending=Decimal("100.00"),
            lower_balance_headroom=Decimal("0.00"),
            required_weekly_savings=Decimal("0.00"),
            cash_based_weekly_limit=Decimal("100.00"),
            weekly_budget_limit=Decimal("60.00"),
            safe_weekly_spending=Decimal("100.00"),
            limiting_factor=SafeSpendingLimitingFactor.WEEKLY_BUDGET,
        )
    safe = SafeWeeklySpending(
        currency=Currency.GBP,
        projection_period=FORECAST_PERIOD,
        forecast_weeks=Decimal("4"),
        expected_forecast_weekly_spending=Decimal("100.00"),
        lower_balance_headroom=Decimal("0.00"),
        required_weekly_savings=Decimal("0.00"),
        cash_based_weekly_limit=Decimal("100.00"),
        weekly_budget_limit=None,
        safe_weekly_spending=Decimal("100.00"),
        limiting_factor=SafeSpendingLimitingFactor.CASH_HEADROOM,
    )
    with pytest.raises(ValidationError, match="must match the selected accounts"):
        FinancialPlanningResult(
            plan=_plan(),
            currency=Currency.GBP,
            balance_projections=(_projection(account_id="other-account"),),
            budgets=(),
            goals=(valid_floor_progress,),
            safe_spending=safe,
            warnings=(),
        )
    other_period = DateRange(start_date=date(2026, 8, 24), end_date=date(2026, 9, 20))
    with pytest.raises(ValidationError, match="one aligned period"):
        FinancialPlanningResult(
            plan=_plan(account_ids=("synthetic-account", "other-account")),
            currency=Currency.GBP,
            balance_projections=(
                _projection(),
                _projection(account_id="other-account", period=other_period),
            ),
            budgets=(),
            goals=(),
            safe_spending=safe,
            warnings=(),
        )
    with pytest.raises(ValidationError, match="safe spending must use"):
        FinancialPlanningResult(
            plan=_plan(),
            currency=Currency.GBP,
            balance_projections=(_projection(),),
            budgets=(),
            goals=(),
            safe_spending=safe.model_copy(update={"projection_period": FORECAST_WEEK}),
            warnings=(),
        )
    warning = PlanningWarning(code=PlanningWarningCode.FORECAST_LIMITATION)
    with pytest.raises(ValidationError, match="warnings must be unique"):
        FinancialPlanningResult(
            plan=_plan(),
            currency=Currency.GBP,
            balance_projections=(_projection(),),
            budgets=(),
            goals=(),
            safe_spending=safe,
            warnings=(warning, warning),
        )


@pytest.mark.parametrize(
    ("arguments", "coverage", "projected"),
    [
        ([], "complete", "221.43"),
        (["--incomplete-coverage"], "partial", "unavailable"),
    ],
)
def test_manual_demo_reports_coverage_and_safe_planning_results(
    arguments: list[str],
    coverage: str,
    projected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["cashflow-planning-demo", *arguments])
    demo_main()

    output = capsys.readouterr().out
    assert f"({coverage})" in output
    assert f"food projected month use: {projected}" in output
    assert "required monthly savings contribution: GBP 120.00" in output
    assert "safe weekly spending estimate: GBP 47.30" in output
    assert "financial advice guarantee: false" in output

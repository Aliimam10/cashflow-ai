"""Regression test for CSV-derived dates through coverage-aware dashboard data."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.api.decision_services import (
    calculate_balance_forecast,
    evaluate_forecast_model,
)
from cashflow_ai.balances import assess_financial_data_freshness
from cashflow_ai.demo_data.dashboard import seed_synthetic_dashboard
from cashflow_ai.frontend.forecast_workflow import forecast_request
from cashflow_ai.imports import persist_confirmed_csv, preview_csv
from cashflow_ai.persistence import (
    AccountRepository,
    Base,
    UserProfileRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    CategoryRecord,
    FinancialRoleRecord,
    UserProfileRecord,
)
from cashflow_ai.schemas.analytics import AnalyticsScope, AnalyticsView
from cashflow_ai.schemas.api_decisions import ForecastEvaluationRequest
from cashflow_ai.schemas.csv_imports import (
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
)
from cashflow_ai.schemas.forecasting import ForecastBaselineName
from cashflow_ai.schemas.freshness import FinancialDataMode, FreshnessPolicy
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    ImportContext,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency, FinancialRole

_CONTENT = (
    b"Date,Description,Amount,Balance\n"
    b"2026-08-31,Synthetic rent,-100.00,900.00\n"
    b"2025-09-01,Synthetic income,1000.00,1000.00\n"
)
_IMPORTED_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def test_full_csv_date_bounds_feed_coverage_analytics_and_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical CSV dates must survive preview, import, and dashboard services."""
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Synthetic User",
                base_currency="GBP",
                timezone="UTC",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id="synthetic-account",
                user_profile_id="synthetic-profile",
                name="Synthetic Current",
                account_type="current",
                currency="GBP",
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))

    preview = preview_csv(_CONTENT, "synthetic-history.csv", preview_rows=1)
    period = preview.suggested_statement_period
    assert preview.truncated is True
    assert preview.suggested_date_column == "Date"
    assert period is not None
    assert period.start_date == date(2025, 9, 1)
    assert period.end_date == date(2026, 8, 31)

    monkeypatch.setattr(
        "cashflow_ai.imports.csv_import_service.utc_now",
        lambda: _IMPORTED_AT,
    )
    summary = persist_confirmed_csv(
        factory,
        _CONTENT,
        "synthetic-history.csv",
        mime_type="text/csv",
        plan=CsvImportPlan(
            account_id="synthetic-account",
            account_currency=Currency.GBP,
            statement_context=ImportContext(
                account_id="synthetic-account",
                coverage=StatementCoverage(
                    statement_start_date=period.start_date,
                    statement_end_date=period.end_date,
                    status=CoverageStatus.COMPLETE,
                ),
            ),
            mapping=CsvColumnMapping(
                transaction_date_column="Date",
                description_column="Description",
                signed_amount_column="Amount",
                running_balance_column="Balance",
            ),
        ),
        confirmation=CsvImportConfirmation(
            preview_file_hash=preview.file_hash,
            user_confirmed=True,
            confirmed_at=datetime(2026, 9, 5, 11, 59, tzinfo=UTC),
        ),
    )
    assert summary.new_transactions == 2

    analytics = compute_cash_flow_analytics(
        factory,
        AnalyticsScope(
            user_profile_id="synthetic-profile",
            account_ids=("synthetic-account",),
            period=period,
            view=AnalyticsView.ACCOUNT,
        ),
    )
    assert analytics.coverage.status.value == "complete"
    assert analytics.totals is not None
    assert analytics.observed_transaction_count == 2

    freshness = assess_financial_data_freshness(
        factory,
        account_id="synthetic-account",
        as_of_date=period.end_date,
        policy=FreshnessPolicy(
            max_transaction_age_days=45,
            max_balance_age_days=45,
            max_coverage_age_days=45,
            minimum_contiguous_coverage_days=60,
        ),
    )
    assert freshness.mode is FinancialDataMode.ACTIVE_FORECASTING
    assert freshness.warnings == ()


def test_same_day_synthetic_import_reaches_future_forecast_api_safely() -> None:
    """A click-time cutoff must include a completed import without backdating it."""
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add(
            CategoryRecord(
                id="housing",
                name="Housing",
                parent_id=None,
                taxonomy_version="1.0",
                is_active=True,
            )
        )

    first_monday = date(2026, 1, 5)
    balance_pence = 100_000
    lines = [
        "transaction_date,posting_date,description,amount,balance,currency,"
        "external_id,transaction_type,category,category_id,financial_role"
    ]
    for index in range(12):
        transaction_date = first_monday + timedelta(weeks=index)
        balance_pence -= 1_000
        balance_text = f"{balance_pence // 100}.{balance_pence % 100:02d}"
        lines.append(
            f"{transaction_date},{transaction_date},SYNTHETIC RENT,-10.00,"
            f"{balance_text},GBP,SYN-{index},direct_debit,Housing,"
            "housing,expense"
        )
    content = ("\n".join(lines) + "\n").encode()
    seeded = seed_synthetic_dashboard(factory, content)
    clicked_at = datetime.now(UTC)

    with session_scope(factory) as session:
        profile = session.scalar(select(UserProfileRecord))
        account = session.scalar(select(AccountRecord))
    assert profile is not None
    assert account is not None
    request = forecast_request(
        profile_id=profile.id,
        account_id=account.id,
        as_of_date=seeded.statement_end_date,
        horizon_days=30,
        payday_days=(1, 15),
        knowledge_cutoff_at=clicked_at,
    )

    evaluation = evaluate_forecast_model(
        factory,
        ForecastEvaluationRequest(
            dataset_plan=request.dataset_plan,
            model_policy=request.model_policy,
        ),
    )
    path = calculate_balance_forecast(factory, request)

    assert evaluation.comparison.selected_model is (
        ForecastBaselineName.RECENT_ROLLING_MEAN
    )
    assert path.selected_model is ForecastBaselineName.RECENT_ROLLING_MEAN
    assert len(path.daily_balances) == 30
    assert {warning.value for warning in path.warnings} >= {
        "low_confidence_model",
        "recent_history_gap",
        "limited_residual_history",
    }

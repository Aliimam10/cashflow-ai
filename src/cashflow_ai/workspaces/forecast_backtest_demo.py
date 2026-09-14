"""Synthetic 60-day fit and 30-day forecast evaluation walkthrough."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from cashflow_ai.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from cashflow_ai.schemas.money import MONEY_QUANTUM
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import Currency, FinancialRole
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastRequest,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCreateRequest,
    WorkspaceEditRequest,
    WorkspaceFinalizeRequest,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
)
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace
from cashflow_ai.workspaces.service import (
    WorkspaceUpload,
    create_workspace,
    edit_workspace_rows,
    finalize_workspace,
    review_workspace_uploads,
)
from cashflow_ai.workspaces.store import WorkspaceStore

_START_DATE = date(2026, 6, 17)
_TOTAL_DAYS = 90
_TRAINING_DAYS = 60
_EVALUATION_DAYS: Literal[30] = 30
_STARTING_BALANCE = Decimal("1800.00")
_OUTPUT_DIRECTORY = Path("data/demo/generated/forecast-backtest")
_MASTER_FILENAME = "fictional_revolut_consolidated_v2_master_90_days.csv"
_TRAINING_FILENAME = "fictional_revolut_consolidated_v2_training_60_days.csv"
_EVALUATION_FILENAME = "fictional_revolut_consolidated_v2_evaluation_30_days.csv"
_ZERO = Decimal("0.00")
_GBP_TABLE_HEADERS = (
    "Date",
    "Description",
    "Category",
    "Money in/out",
    "Balance",
    "Tax withheld",
    "Other taxes",
    "Fees",
)
_DUAL_CURRENCY_HEADERS = (
    "Date",
    "Description",
    "Category",
    "Money in/out",
    "Money in/out",
    "Balance",
    "Balance",
    "Tax withheld",
    "Tax withheld",
    "Other taxes",
    "Other taxes",
    "Fees",
    "Fees",
)
_CSV_WIDTH = len(_DUAL_CURRENCY_HEADERS)
_MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True, slots=True)
class SyntheticStatementRow:
    """One fictional daily statement row with explicit ground truth."""

    transaction_date: date
    description: str
    amount: Decimal
    balance_after: Decimal
    category_id: str
    financial_role: FinancialRole


@dataclass(frozen=True, slots=True)
class ForecastBacktestMetrics:
    """Holdout metrics calculated only after the forecast has been produced."""

    evaluated_days: int
    interval_days_covered: int
    interval_coverage_percent: Decimal
    daily_mae: Decimal
    daily_rmse: Decimal
    final_expected_balance: Decimal
    final_actual_balance: Decimal
    final_error: Decimal
    final_lower_balance: Decimal
    final_upper_balance: Decimal
    final_actual_in_interval: bool


@dataclass(frozen=True, slots=True)
class ForecastBacktestRun:
    """Auditable paths, cutoff, forecast and scores from one synthetic run."""

    master_path: Path
    training_path: Path
    evaluation_path: Path
    training_start: date
    training_end: date
    evaluation_start: date
    evaluation_end: date
    training_row_count: int
    evaluation_row_count: int
    parser_name: str
    layout_version: str
    excluded_non_gbp_rows: int
    source_exclusions_confirmed: bool
    forecast: WorkspaceForecastAvailable
    metrics: ForecastBacktestMetrics


_BASE_ACTIVITY: dict[int, tuple[str, Decimal, str, FinancialRole]] = {
    0: (
        "FICTIONAL WEEKLY RENT",
        Decimal("-165.00"),
        "rent_housing",
        FinancialRole.EXPENSE,
    ),
    1: (
        "FICTIONAL GROCERIES",
        Decimal("-66.00"),
        "groceries",
        FinancialRole.EXPENSE,
    ),
    2: (
        "FICTIONAL TRANSPORT",
        Decimal("-18.00"),
        "transport",
        FinancialRole.EXPENSE,
    ),
    3: (
        "FICTIONAL HOUSEHOLD BILLS",
        Decimal("-34.00"),
        "bills",
        FinancialRole.EXPENSE,
    ),
    4: (
        "FICTIONAL WEEKLY PAY",
        Decimal("430.00"),
        "income",
        FinancialRole.INCOME,
    ),
    5: (
        "FICTIONAL RESTAURANT",
        Decimal("-48.00"),
        "restaurants",
        FinancialRole.EXPENSE,
    ),
    6: (
        "FICTIONAL ENTERTAINMENT",
        Decimal("-22.00"),
        "entertainment",
        FinancialRole.EXPENSE,
    ),
}

_FOUR_WEEK_VARIATION: dict[int, tuple[Decimal, ...]] = {
    0: (Decimal("0.00"), Decimal("-5.00"), Decimal("3.00"), Decimal("2.00")),
    1: (Decimal("-4.00"), Decimal("2.00"), Decimal("-1.00"), Decimal("3.00")),
    2: (Decimal("0.00"), Decimal("-2.00"), Decimal("1.00"), Decimal("1.00")),
    3: (Decimal("-3.00"), Decimal("2.00"), Decimal("-1.00"), Decimal("2.00")),
    4: (Decimal("-10.00"), Decimal("0.00"), Decimal("10.00"), Decimal("0.00")),
    5: (Decimal("-6.00"), Decimal("3.00"), Decimal("-2.00"), Decimal("5.00")),
    6: (Decimal("0.00"), Decimal("-3.00"), Decimal("1.00"), Decimal("2.00")),
}

_CATEGORY_DISPLAY = {
    "rent_housing": "Rent",
    "groceries": "Groceries",
    "transport": "Transport",
    "bills": "Bills",
    "income": "Income",
    "restaurants": "Restaurants",
    "entertainment": "Entertainment",
}
_CATEGORY_GROUND_TRUTH = {
    display: (
        category_id,
        FinancialRole.INCOME if category_id == "income" else FinancialRole.EXPENSE,
    )
    for category_id, display in _CATEGORY_DISPLAY.items()
}


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _generate_statement() -> tuple[SyntheticStatementRow, ...]:
    """Generate 90 contiguous fictional days with a reproducible weekly pattern."""
    balance = _STARTING_BALANCE
    rows: list[SyntheticStatementRow] = []
    for offset in range(_TOTAL_DAYS):
        transaction_date = _START_DATE + timedelta(days=offset)
        weekday = transaction_date.weekday()
        description, base_amount, category_id, role = _BASE_ACTIVITY[weekday]
        week_index = offset // 7
        variation = _FOUR_WEEK_VARIATION[weekday][week_index % 4]
        amount = _money(base_amount + variation)
        balance = _money(balance + amount)
        rows.append(
            SyntheticStatementRow(
                transaction_date=transaction_date,
                description=description,
                amount=amount,
                balance_after=balance,
                category_id=category_id,
                financial_role=role,
            )
        )
    return tuple(rows)


def _padded(*values: str) -> list[str]:
    return [*values, *("" for _ in range(_CSV_WIDTH - len(values)))]


def _consolidated_date(value: date) -> str:
    return f"{_MONTH_NAMES[value.month - 1]} {value.day}, {value.year}"


def _currency_text(value: Decimal, symbol: str) -> str:
    sign = "-" if value < _ZERO else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def _write_consolidated_statement(
    path: Path,
    rows: tuple[SyntheticStatementRow, ...],
) -> None:
    """Write one fictional consolidated-v2 layout with a secondary EUR table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerows(
            (
                _padded("Fictional consolidated statement"),
                _padded(
                    "Statement period",
                    _consolidated_date(rows[0].transaction_date),
                    _consolidated_date(rows[-1].transaction_date),
                ),
                _padded(),
                _padded(*_GBP_TABLE_HEADERS),
            )
        )
        writer.writerows(
            _padded(
                _consolidated_date(row.transaction_date),
                row.description,
                _CATEGORY_DISPLAY[row.category_id],
                _currency_text(row.amount, "£"),
                _currency_text(row.balance_after, "£"),
                "£0.00",
                "£0.00",
                "£0.00",
            )
            for row in rows
        )
        writer.writerows(
            (
                _padded(
                    "Total",
                    "",
                    "",
                    _currency_text(sum((row.amount for row in rows), start=_ZERO), "£"),
                ),
                _padded(),
                _padded("Fictional non-GBP section"),
                list(_DUAL_CURRENCY_HEADERS),
                _padded(
                    _consolidated_date(rows[-1].transaction_date),
                    "FICTIONAL EUR CARD PURCHASE",
                    "Card",
                    "-€12.00",
                    "-£10.50",
                    "€88.00",
                    "£77.00",
                    "€0.00",
                    "£0.00",
                    "€0.00",
                    "£0.00",
                    "€0.00",
                    "£0.00",
                ),
                _padded("Total", "", "", "-€12.00", "-£10.50"),
            )
        )


def _review_and_finalize_training(
    training_path: Path,
    rows: tuple[SyntheticStatementRow, ...],
) -> tuple[StatementWorkspace, str, str, int]:
    """Run the fictional training split through the real workspace lifecycle."""
    training_start = rows[0].transaction_date
    training_end = rows[-1].transaction_date
    created_at = datetime(
        training_end.year,
        training_end.month,
        training_end.day,
        9,
        tzinfo=UTC,
    )
    store = WorkspaceStore()
    workspace = create_workspace(
        store,
        WorkspaceCreateRequest(
            retention_mode=WorkspaceRetentionMode.TEMPORARY,
            account_name="Fictional forecast account",
        ),
        now=created_at,
    )
    review = review_workspace_uploads(
        store,
        workspace.workspace_id,
        (
            WorkspaceUpload(
                filename=training_path.name,
                content=training_path.read_bytes(),
                mime_type="text/csv",
            ),
        ),
        now=created_at + timedelta(minutes=1),
    )
    source = review.workspace.sources[0]
    adapter_evidence = (
        review.accepted_files,
        source.parser_name,
        source.layout_version,
        source.warning_codes,
        source.excluded_transaction_rows,
    )
    if adapter_evidence != (
        1,
        "revolut_consolidated_csv",
        "consolidated_v2_gbp_1",
        ("non_gbp_transaction_sections_excluded",),
        1,
    ):
        raise RuntimeError("synthetic consolidated-v2 adapter evidence is incomplete")

    revisions = tuple(
        WorkspaceRowRevision(
            row_id=row.row_id,
            expected_revision=row.revision,
            transaction_date=row.transaction_date,
            description=row.description,
            amount=row.amount,
            balance_after=row.balance_after,
            category_id=_CATEGORY_GROUND_TRUTH[row.transaction_type or ""][0],
            financial_role=_CATEGORY_GROUND_TRUTH[row.transaction_type or ""][1],
            review_state=WorkspaceRowReviewState.CONFIRMED,
        )
        for row in review.workspace.rows
    )
    edited = edit_workspace_rows(
        store,
        workspace.workspace_id,
        WorkspaceEditRequest(
            expected_workspace_revision=review.workspace.revision,
            rows=revisions,
        ),
        now=created_at + timedelta(minutes=2),
    )
    finalize_request = WorkspaceFinalizeRequest(
        expected_workspace_revision=edited.revision,
        statement_confirmed=True,
        date_interpretation_confirmed=True,
        sign_convention_confirmed=True,
        source_exclusions_confirmed=True,
        coverage=WorkspaceCoverageConfirmation(
            start_date=training_start,
            end_date=training_end,
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=rows[-1].balance_after,
            as_of_date=training_end,
            currency=Currency.GBP,
            confirmed=True,
        ),
    )
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    try:
        finalized = finalize_workspace(
            store,
            create_session_factory(engine),
            workspace.workspace_id,
            finalize_request,
            now=created_at + timedelta(minutes=3),
        ).workspace
    finally:
        engine.dispose()
    return (
        finalized,
        "revolut_consolidated_csv",
        "consolidated_v2_gbp_1",
        source.excluded_transaction_rows,
    )


def _score_forecast(
    forecast: WorkspaceForecastAvailable,
    evaluation_rows: tuple[SyntheticStatementRow, ...],
) -> ForecastBacktestMetrics:
    """Reveal held-out actuals and compare them with the already-built path."""
    actual_by_date = {
        row.transaction_date: row.balance_after for row in evaluation_rows
    }
    paired = tuple(
        (point, actual_by_date[point.forecast_date])
        for point in forecast.daily_balances
    )
    errors = tuple(point.expected_balance - actual for point, actual in paired)
    evaluated_days = len(paired)
    mae = sum((abs(error) for error in errors), start=_ZERO) / evaluated_days
    mean_squared_error = (
        sum((error * error for error in errors), start=_ZERO) / evaluated_days
    )
    interval_days_covered = sum(
        point.lower_balance <= actual <= point.upper_balance for point, actual in paired
    )
    final_point, final_actual = paired[-1]
    return ForecastBacktestMetrics(
        evaluated_days=evaluated_days,
        interval_days_covered=interval_days_covered,
        interval_coverage_percent=_money(
            Decimal(interval_days_covered) / Decimal(evaluated_days) * Decimal("100")
        ),
        daily_mae=_money(mae),
        daily_rmse=_money(mean_squared_error.sqrt()),
        final_expected_balance=final_point.expected_balance,
        final_actual_balance=final_actual,
        final_error=_money(final_point.expected_balance - final_actual),
        final_lower_balance=final_point.lower_balance,
        final_upper_balance=final_point.upper_balance,
        final_actual_in_interval=(
            final_point.lower_balance <= final_actual <= final_point.upper_balance
        ),
    )


def run_backtest(output_directory: Path = _OUTPUT_DIRECTORY) -> ForecastBacktestRun:
    """Finalize days 1-60, forecast, then reveal days 61-90 for scoring."""
    all_rows = _generate_statement()
    training_rows = all_rows[:_TRAINING_DAYS]
    evaluation_rows = all_rows[_TRAINING_DAYS:]
    master_path = output_directory / _MASTER_FILENAME
    training_path = output_directory / _TRAINING_FILENAME
    evaluation_path = output_directory / _EVALUATION_FILENAME
    _write_consolidated_statement(training_path, training_rows)

    workspace, parser_name, layout_version, excluded_non_gbp_rows = (
        _review_and_finalize_training(training_path, training_rows)
    )
    training_end = training_rows[-1].transaction_date
    forecast = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=workspace.revision,
            horizon_days=_EVALUATION_DAYS,
        ),
        today=training_end,
    )
    if not isinstance(forecast, WorkspaceForecastAvailable):
        raise RuntimeError("synthetic 60-day history unexpectedly withheld forecast")

    # These later rows and the 90-day master are created only after forecasting.
    _write_consolidated_statement(evaluation_path, evaluation_rows)
    _write_consolidated_statement(master_path, all_rows)
    metrics = _score_forecast(forecast, evaluation_rows)
    return ForecastBacktestRun(
        master_path=master_path,
        training_path=training_path,
        evaluation_path=evaluation_path,
        training_start=training_rows[0].transaction_date,
        training_end=training_end,
        evaluation_start=evaluation_rows[0].transaction_date,
        evaluation_end=evaluation_rows[-1].transaction_date,
        training_row_count=len(training_rows),
        evaluation_row_count=len(evaluation_rows),
        parser_name=parser_name,
        layout_version=layout_version,
        excluded_non_gbp_rows=excluded_non_gbp_rows,
        source_exclusions_confirmed=True,
        forecast=forecast,
        metrics=metrics,
    )


def _pounds(value: Decimal) -> str:
    sign = "-" if value < _ZERO else ""
    return f"{sign}£{abs(value):,.2f}"


def main() -> None:
    """Generate the fictional split and print an auditable holdout comparison."""
    run = run_backtest()
    metrics = run.metrics
    final_inside = "yes" if metrics.final_actual_in_interval else "no"
    print("CashFlow AI synthetic 60/30 forecast backtest")
    print(f"master 90-day CSV: {run.master_path}")
    print(f"training CSV: {run.training_path}")
    print(f"evaluation CSV: {run.evaluation_path}")
    print(
        f"training window: {run.training_start} to {run.training_end} "
        f"({run.training_row_count} days)"
    )
    print(
        f"hidden evaluation: {run.evaluation_start} to {run.evaluation_end} "
        f"({run.evaluation_row_count} days)"
    )
    print(
        "leakage check: the forecast received 60 training rows; "
        "30 later rows were revealed only for scoring"
    )
    print(f"adapter: {run.parser_name} / {run.layout_version}")
    print(
        "non-GBP exclusion review: "
        f"confirmed ({run.excluded_non_gbp_rows} fictional row excluded)"
    )
    print(f"model fit: {run.forecast.model.model_name.value}")
    print(f"30-day expected closing balance: {_pounds(metrics.final_expected_balance)}")
    print(f"30-day actual closing balance: {_pounds(metrics.final_actual_balance)}")
    print(f"final balance error (forecast - actual): {_pounds(metrics.final_error)}")
    print(f"daily MAE: {_pounds(metrics.daily_mae)}")
    print(f"daily RMSE: {_pounds(metrics.daily_rmse)}")
    print(
        "80% interval coverage: "
        f"{metrics.interval_coverage_percent:.2f}% "
        f"({metrics.interval_days_covered}/{metrics.evaluated_days} days)"
    )
    print(f"actual final balance inside 80% interval: {final_inside}")
    print("privacy: fictional data only; generated CSVs are Git-ignored")


__all__ = [
    "ForecastBacktestMetrics",
    "ForecastBacktestRun",
    "SyntheticStatementRow",
    "main",
    "run_backtest",
]

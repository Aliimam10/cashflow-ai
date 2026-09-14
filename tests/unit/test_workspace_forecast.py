"""Tests for conservative forecasts over finalized statement workspaces."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastHorizon,
    WorkspaceForecastModelMetadata,
    WorkspaceForecastPoint,
    WorkspaceForecastReasonCode,
    WorkspaceForecastRequest,
    WorkspaceForecastResult,
    WorkspaceForecastWarningCode,
    WorkspaceForecastWithheld,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)
from cashflow_ai.workspaces import forecasting as forecast_module
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace

_TODAY = date(2026, 9, 13)
_FINALIZED_AT = datetime(2026, 9, 13, 8, tzinfo=UTC)
_DEFAULT = object()


def _row(
    *,
    row_id: str = "synthetic-row",
    transaction_date: date = _TODAY - timedelta(days=7),
    posting_date: date | None = None,
    amount: Decimal = Decimal("-14.00"),
    role: FinancialRole = FinancialRole.EXPENSE,
    state: WorkspaceRowReviewState = WorkspaceRowReviewState.CONFIRMED,
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id=row_id,
        transaction_date=transaction_date,
        posting_date=posting_date,
        description="SYNTHETIC TRANSACTION",
        amount=amount,
        category_id="other",
        financial_role=role,
        review_state=state,
    )


def _workspace(
    *,
    today: date = _TODAY,
    revision: int = 3,
    coverage_days: int = 70,
    coverage_end: date | None = None,
    coverage_status: CoverageStatus = CoverageStatus.COMPLETE,
    missing_periods: tuple[DateRange, ...] = (),
    rows: tuple[WorkspaceTransactionRow, ...] = (),
    balance: WorkspaceBalanceConfirmation | object | None = _DEFAULT,
    finalized_at: datetime = _FINALIZED_AT,
) -> StatementWorkspace:
    end = coverage_end or today
    coverage = WorkspaceCoverageConfirmation(
        start_date=end - timedelta(days=coverage_days - 1),
        end_date=end,
        status=coverage_status,
        missing_periods=missing_periods,
        confirmed=True,
    )
    resolved_balance = (
        WorkspaceBalanceConfirmation(
            balance=Decimal("1000.00"),
            as_of_date=today,
            confirmed=True,
        )
        if balance is _DEFAULT
        else cast(WorkspaceBalanceConfirmation | None, balance)
    )
    return StatementWorkspace(
        workspace_id="synthetic-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Synthetic account",
        revision=revision,
        rows=rows,
        coverage=coverage,
        balance=resolved_balance,
        created_at=finalized_at - timedelta(hours=2),
        updated_at=finalized_at - timedelta(hours=1),
        finalized_at=finalized_at,
    )


def _request(horizon: int = 15, *, revision: int = 3) -> WorkspaceForecastRequest:
    return WorkspaceForecastRequest(
        expected_workspace_revision=revision,
        horizon_days=cast(WorkspaceForecastHorizon, horizon),
    )


def _available(
    workspace: StatementWorkspace,
    *,
    horizon: int = 15,
    today: date = _TODAY,
) -> WorkspaceForecastAvailable:
    result = forecast_statement_workspace(
        workspace,
        _request(horizon, revision=workspace.revision),
        today=today,
    )
    assert isinstance(result, WorkspaceForecastAvailable)
    return result


@pytest.mark.parametrize("horizon", [15, 30, 60, 90])
def test_supported_horizons_return_exact_tomorrow_first_paths(horizon: int) -> None:
    result = _available(_workspace(), horizon=horizon)

    assert result.horizon_days == horizon
    assert len(result.daily_balances) == horizon
    assert result.daily_balances[0].forecast_date == _TODAY + timedelta(days=1)
    assert result.daily_balances[-1].forecast_date == _TODAY + timedelta(days=horizon)
    assert result.confirmed_balance == Decimal("1000.00")
    assert result.confirmed_balance_as_of == _TODAY
    assert result.expected_balance_as_of_today == Decimal("1000.00")
    assert result.model.advanced_model_selected is False
    assert result.model.historical_backtest_performed is False
    assert result.model.knowledge_cutoff_at == _FINALIZED_AT
    assert result.model.training_window_start == _TODAY - timedelta(days=59)
    assert "without claiming" in result.model.selection_reason
    assert result.warnings == (
        WorkspaceForecastWarningCode.ONE_SHOT_WORKSPACE_BASELINE,
        WorkspaceForecastWarningCode.EMPIRICAL_INTERVAL_ESTIMATE,
        WorkspaceForecastWarningCode.UNCLASSIFIED_TOTAL_CASH_FLOW,
    )


@pytest.mark.parametrize("horizon", [1, 14, 16, 365])
def test_request_rejects_unsupported_horizons(horizon: int) -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        _request(horizon)


def test_forecast_is_reproducible_and_flat_history_keeps_uncertainty() -> None:
    workspace = _workspace()

    first = _available(workspace, horizon=90)
    second = _available(workspace, horizon=90)

    assert first == second
    assert first.daily_balances[0].lower_balance < Decimal("1000.00")
    assert first.daily_balances[0].upper_balance > Decimal("1000.00")
    assert first.daily_balances[-1].lower_balance < Decimal("1000.00")
    assert first.daily_balances[-1].upper_balance > Decimal("1000.00")
    assert forecast_module._quantile((Decimal("7.00"),), Decimal("0.50")) == (
        Decimal("7.00")
    )


def _weekday_rows(
    role: FinancialRole,
    amount: Decimal,
) -> tuple[WorkspaceTransactionRow, ...]:
    training_start = _TODAY - timedelta(days=59)
    target_weekday = (_TODAY + timedelta(days=1)).weekday()
    dates = tuple(
        training_start + timedelta(days=offset)
        for offset in range(60)
        if (training_start + timedelta(days=offset)).weekday() == target_weekday
    )
    return tuple(
        _row(
            row_id=f"synthetic-{role.value}-{index}",
            transaction_date=transaction_date,
            amount=amount,
            role=role,
        )
        for index, transaction_date in enumerate(dates)
    )


@pytest.mark.parametrize(
    ("role", "amount", "expected_direction"),
    [
        (FinancialRole.INCOME, Decimal("10.00"), 1),
        (FinancialRole.REFUND, Decimal("10.00"), 1),
        (FinancialRole.REIMBURSEMENT, Decimal("10.00"), 1),
        (FinancialRole.TRANSFER_IN, Decimal("10.00"), 1),
        (FinancialRole.EXPENSE, Decimal("-10.00"), -1),
        (FinancialRole.TRANSFER_OUT, Decimal("-10.00"), -1),
        (FinancialRole.CASH_WITHDRAWAL, Decimal("-10.00"), -1),
        (FinancialRole.EXCLUDED, Decimal("-10.00"), 0),
    ],
)
def test_balance_baseline_includes_every_cash_role_except_excluded(
    role: FinancialRole,
    amount: Decimal,
    expected_direction: int,
) -> None:
    workspace = _workspace(rows=_weekday_rows(role, amount))

    result = _available(workspace)
    first = result.daily_balances[0].expected_balance

    if expected_direction > 0:
        assert first > result.expected_balance_as_of_today
    elif expected_direction < 0:
        assert first < result.expected_balance_as_of_today
    else:
        assert first == result.expected_balance_as_of_today


def test_unknown_financial_role_withholds_without_private_values() -> None:
    workspace = _workspace(
        rows=(_row(role=FinancialRole.UNKNOWN, amount=Decimal("-77.00")),)
    )

    result = forecast_statement_workspace(workspace, _request(), today=_TODAY)

    assert isinstance(result, WorkspaceForecastWithheld)
    assert WorkspaceForecastReasonCode.UNKNOWN_FINANCIAL_ROLES in result.reasons
    serialized = result.model_dump_json()
    assert "SYNTHETIC TRANSACTION" not in serialized
    assert "77.00" not in serialized
    assert "Synthetic account" not in serialized


def test_revision_mismatch_withholds_the_path() -> None:
    result = forecast_statement_workspace(
        _workspace(), _request(revision=2), today=_TODAY
    )

    assert isinstance(result, WorkspaceForecastWithheld)
    assert result.reasons == (WorkspaceForecastReasonCode.REVISION_MISMATCH,)


def test_draft_and_defensively_unresolved_rows_are_withheld() -> None:
    draft = StatementWorkspace(
        workspace_id="synthetic-draft",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.DRAFT,
        account_name="Synthetic account",
        revision=3,
        created_at=_FINALIZED_AT,
        updated_at=_FINALIZED_AT,
    )
    unresolved = _workspace().model_copy(
        update={"rows": (_row(state=WorkspaceRowReviewState.READY),)}
    )

    draft_result = forecast_statement_workspace(draft, _request(), today=_TODAY)
    unresolved_result = forecast_statement_workspace(
        unresolved, _request(), today=_TODAY
    )

    assert isinstance(draft_result, WorkspaceForecastWithheld)
    assert WorkspaceForecastReasonCode.WORKSPACE_NOT_FINALIZED in draft_result.reasons
    assert WorkspaceForecastReasonCode.BALANCE_REQUIRED in draft_result.reasons
    assert isinstance(unresolved_result, WorkspaceForecastWithheld)
    assert unresolved_result.reasons == (WorkspaceForecastReasonCode.UNRESOLVED_ROWS,)


def test_missing_and_non_latest_balances_are_withheld() -> None:
    no_balance = _workspace(balance=None)
    old_balance = WorkspaceBalanceConfirmation(
        balance=Decimal("900.00"),
        as_of_date=_TODAY - timedelta(days=1),
        confirmed=True,
    )
    non_latest = _workspace(
        rows=(_row(transaction_date=_TODAY),),
        balance=old_balance,
    )

    missing_result = forecast_statement_workspace(no_balance, _request(), today=_TODAY)
    non_latest_result = forecast_statement_workspace(
        non_latest, _request(), today=_TODAY
    )

    assert isinstance(missing_result, WorkspaceForecastWithheld)
    assert missing_result.reasons == (WorkspaceForecastReasonCode.BALANCE_REQUIRED,)
    assert isinstance(non_latest_result, WorkspaceForecastWithheld)
    assert non_latest_result.reasons == (
        WorkspaceForecastReasonCode.BALANCE_NOT_LATEST,
    )


@pytest.mark.parametrize(
    "evidence", ["finalized", "coverage", "balance", "row", "posting"]
)
def test_future_dated_evidence_is_withheld(evidence: str) -> None:
    workspace = _workspace()
    tomorrow = _TODAY + timedelta(days=1)
    if evidence == "finalized":
        workspace = workspace.model_copy(
            update={"finalized_at": _FINALIZED_AT + timedelta(days=1)}
        )
    elif evidence == "coverage":
        workspace = workspace.model_copy(
            update={
                "coverage": WorkspaceCoverageConfirmation(
                    start_date=tomorrow - timedelta(days=69),
                    end_date=tomorrow,
                    status=CoverageStatus.COMPLETE,
                    confirmed=True,
                )
            }
        )
    elif evidence == "balance":
        workspace = workspace.model_copy(
            update={
                "balance": WorkspaceBalanceConfirmation(
                    balance=Decimal("1000.00"),
                    as_of_date=tomorrow,
                    confirmed=True,
                )
            }
        )
    elif evidence == "row":
        workspace = workspace.model_copy(
            update={"rows": (_row(transaction_date=tomorrow),)}
        )
    else:
        workspace = workspace.model_copy(
            update={"rows": (_row(posting_date=tomorrow),)}
        )

    result = forecast_statement_workspace(workspace, _request(), today=_TODAY)

    assert isinstance(result, WorkspaceForecastWithheld)
    assert WorkspaceForecastReasonCode.FUTURE_DATED_EVIDENCE in result.reasons


def test_stale_balance_and_stale_coverage_are_independent_gates() -> None:
    old_balance = WorkspaceBalanceConfirmation(
        balance=Decimal("1000.00"),
        as_of_date=_TODAY - timedelta(days=46),
        confirmed=True,
    )
    stale_balance = _workspace(balance=old_balance)
    stale_coverage = _workspace(coverage_end=_TODAY - timedelta(days=46))

    balance_result = forecast_statement_workspace(
        stale_balance, _request(), today=_TODAY
    )
    coverage_result = forecast_statement_workspace(
        stale_coverage, _request(), today=_TODAY
    )

    assert isinstance(balance_result, WorkspaceForecastWithheld)
    assert WorkspaceForecastReasonCode.BALANCE_STALE in balance_result.reasons
    assert WorkspaceForecastReasonCode.COVERAGE_STALE not in balance_result.reasons
    assert isinstance(coverage_result, WorkspaceForecastWithheld)
    assert WorkspaceForecastReasonCode.COVERAGE_STALE in coverage_result.reasons
    assert WorkspaceForecastReasonCode.BALANCE_STALE not in coverage_result.reasons


def test_forty_five_day_boundary_is_available_and_discloses_bridge() -> None:
    end = _TODAY - timedelta(days=45)
    balance = WorkspaceBalanceConfirmation(
        balance=Decimal("1000.00"), as_of_date=end, confirmed=True
    )
    workspace = _workspace(coverage_end=end, balance=balance)

    result = _available(workspace)

    assert WorkspaceForecastWarningCode.BALANCE_BRIDGED_TO_TODAY in result.warnings
    assert result.confirmed_balance_as_of == end
    assert result.expected_balance_as_of_today == Decimal("1000.00")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CoverageStatus.PARTIAL, WorkspaceForecastReasonCode.PARTIAL_COVERAGE),
        (CoverageStatus.UNKNOWN, WorkspaceForecastReasonCode.UNKNOWN_COVERAGE),
    ],
)
def test_untrustworthy_coverage_status_withholds(
    status: CoverageStatus,
    reason: WorkspaceForecastReasonCode,
) -> None:
    workspace = _workspace(coverage_status=status)

    result = forecast_statement_workspace(workspace, _request(), today=_TODAY)

    assert isinstance(result, WorkspaceForecastWithheld)
    assert result.reasons == (reason,)


def test_recent_gap_withholds_but_a_gap_older_than_training_window_passes() -> None:
    recent_gap = DateRange(
        start_date=_TODAY - timedelta(days=20),
        end_date=_TODAY - timedelta(days=18),
    )
    old_gap = DateRange(
        start_date=_TODAY - timedelta(days=90),
        end_date=_TODAY - timedelta(days=85),
    )
    recent = _workspace(
        coverage_days=100,
        coverage_status=CoverageStatus.GAPPED,
        missing_periods=(recent_gap,),
    )
    old = _workspace(
        coverage_days=100,
        coverage_status=CoverageStatus.GAPPED,
        missing_periods=(old_gap,),
    )

    recent_result = forecast_statement_workspace(recent, _request(), today=_TODAY)
    old_result = _available(old)

    assert isinstance(recent_result, WorkspaceForecastWithheld)
    assert recent_result.reasons == (WorkspaceForecastReasonCode.RECENT_COVERAGE_GAP,)
    assert old_result.model.training_window_start > old_gap.end_date


def test_less_than_sixty_days_of_history_withholds() -> None:
    result = forecast_statement_workspace(
        _workspace(coverage_days=59), _request(), today=_TODAY
    )

    assert isinstance(result, WorkspaceForecastWithheld)
    assert result.reasons == (WorkspaceForecastReasonCode.INSUFFICIENT_HISTORY,)


def test_known_covered_bridge_days_do_not_invent_activity() -> None:
    balance = WorkspaceBalanceConfirmation(
        balance=Decimal("1000.00"),
        as_of_date=_TODAY - timedelta(days=10),
        confirmed=True,
    )
    workspace = _workspace(balance=balance)

    result = _available(workspace)

    assert result.expected_balance_as_of_today == Decimal("1000.00")
    assert WorkspaceForecastWarningCode.BALANCE_BRIDGED_TO_TODAY in result.warnings


def test_default_date_uses_the_injected_london_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forecast_module, "_calendar_today", lambda: _TODAY)

    result = forecast_statement_workspace(_workspace(), _request())

    assert isinstance(result, WorkspaceForecastAvailable)
    assert result.as_of_date == _TODAY


def test_result_contract_rejects_invalid_intervals_paths_and_metadata() -> None:
    with pytest.raises(ValidationError, match="interval must contain"):
        WorkspaceForecastPoint(
            forecast_date=_TODAY + timedelta(days=1),
            expected_balance=Decimal("100.00"),
            lower_balance=Decimal("101.00"),
            upper_balance=Decimal("102.00"),
        )
    with pytest.raises(ValidationError, match="must be sixty days"):
        WorkspaceForecastModelMetadata(
            selection_reason="Synthetic baseline",
            training_window_start=_TODAY - timedelta(days=58),
            training_window_end=_TODAY,
            knowledge_cutoff_at=_FINALIZED_AT,
            interval_probability=Decimal("0.80"),
            simulation_count=500,
            random_seed=42,
            minimum_daily_uncertainty=Decimal("1.00"),
        )

    valid = _available(_workspace())
    wrong_dates = valid.model_dump()
    wrong_dates["daily_balances"][0]["forecast_date"] = _TODAY
    with pytest.raises(ValidationError, match="tomorrow through"):
        WorkspaceForecastAvailable.model_validate(wrong_dates)
    missing_warning = valid.model_dump()
    missing_warning["warnings"] = (
        WorkspaceForecastWarningCode.EMPIRICAL_INTERVAL_ESTIMATE,
        WorkspaceForecastWarningCode.UNCLASSIFIED_TOTAL_CASH_FLOW,
        WorkspaceForecastWarningCode.BALANCE_BRIDGED_TO_TODAY,
    )
    with pytest.raises(ValidationError, match="disclose baseline"):
        WorkspaceForecastAvailable.model_validate(missing_warning)


def test_discriminated_result_contract_accepts_both_outcomes() -> None:
    adapter: TypeAdapter[WorkspaceForecastResult] = TypeAdapter(WorkspaceForecastResult)
    available = _available(_workspace())
    withheld = forecast_statement_workspace(
        _workspace(), _request(revision=1), today=_TODAY
    )

    assert adapter.validate_python(available.model_dump()) == available
    assert isinstance(withheld, WorkspaceForecastWithheld)
    assert adapter.validate_python(withheld.model_dump()) == withheld

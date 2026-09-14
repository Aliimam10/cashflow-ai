"""Typed results for conservative forecasts over finalized statement workspaces."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import Currency, Identifier

WorkspaceForecastHorizon = Literal[15, 30, 60, 90]


class WorkspaceForecastStatus(StrEnum):
    """Whether trustworthy workspace evidence permitted a balance path."""

    AVAILABLE = "available"
    WITHHELD = "withheld"


class WorkspaceForecastReasonCode(StrEnum):
    """Stable, privacy-safe reasons that prevent a workspace forecast."""

    REVISION_MISMATCH = "revision_mismatch"
    WORKSPACE_NOT_FINALIZED = "workspace_not_finalized"
    UNRESOLVED_ROWS = "unresolved_rows"
    UNKNOWN_FINANCIAL_ROLES = "unknown_financial_roles"
    BALANCE_REQUIRED = "balance_required"
    BALANCE_NOT_LATEST = "balance_not_latest"
    FUTURE_DATED_EVIDENCE = "future_dated_evidence"
    BALANCE_STALE = "balance_stale"
    COVERAGE_STALE = "coverage_stale"
    PARTIAL_COVERAGE = "partial_coverage"
    UNKNOWN_COVERAGE = "unknown_coverage"
    RECENT_COVERAGE_GAP = "recent_coverage_gap"
    INSUFFICIENT_HISTORY = "insufficient_history"


class WorkspaceForecastWarningCode(StrEnum):
    """Visible limitations attached to every generated workspace forecast."""

    ONE_SHOT_WORKSPACE_BASELINE = "one_shot_workspace_baseline"
    EMPIRICAL_INTERVAL_ESTIMATE = "empirical_interval_estimate"
    UNCLASSIFIED_TOTAL_CASH_FLOW = "unclassified_total_cash_flow"
    BALANCE_BRIDGED_TO_TODAY = "balance_bridged_to_today"


class WorkspaceForecastModelName(StrEnum):
    """Only the honest model available for a newly finalized workspace."""

    RECENT_60_DAY_WEEKDAY_MEAN = "recent_60_day_weekday_mean"


class WorkspaceForecastIntervalMethod(StrEnum):
    """Uncertainty method used by the conservative workspace baseline."""

    EMPIRICAL_RESIDUAL_BOOTSTRAP = "empirical_residual_bootstrap"


class _WorkspaceForecastContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class WorkspaceForecastRequest(_WorkspaceForecastContract):
    """Revision-bound user controls; financial evidence stays server-owned."""

    expected_workspace_revision: int = Field(ge=1)
    horizon_days: WorkspaceForecastHorizon


class WorkspaceForecastPoint(_WorkspaceForecastContract):
    """One future daily balance with an explicitly estimated interval."""

    forecast_date: date
    expected_balance: Money
    lower_balance: Money
    upper_balance: Money

    @model_validator(mode="after")
    def validate_interval(self) -> WorkspaceForecastPoint:
        """Require every expected balance to remain inside its interval."""
        if not self.lower_balance <= self.expected_balance <= self.upper_balance:
            raise ValueError("workspace forecast interval must contain its estimate")
        return self


class WorkspaceForecastModelMetadata(_WorkspaceForecastContract):
    """Reproducibility metadata without a fabricated historical backtest."""

    model_name: Literal[WorkspaceForecastModelName.RECENT_60_DAY_WEEKDAY_MEAN] = (
        WorkspaceForecastModelName.RECENT_60_DAY_WEEKDAY_MEAN
    )
    advanced_model_selected: Literal[False] = False
    historical_backtest_performed: Literal[False] = False
    selection_reason: str = Field(min_length=1, max_length=500)
    training_window_start: date
    training_window_end: date
    training_days: Literal[60] = 60
    knowledge_cutoff_at: AwareDatetime
    interval_method: Literal[
        WorkspaceForecastIntervalMethod.EMPIRICAL_RESIDUAL_BOOTSTRAP
    ] = WorkspaceForecastIntervalMethod.EMPIRICAL_RESIDUAL_BOOTSTRAP
    interval_probability: Decimal = Field(gt=Decimal("0.50"), lt=Decimal("1"))
    simulation_count: int = Field(ge=100, le=20_000)
    random_seed: int = Field(ge=0)
    minimum_daily_uncertainty: Money = Field(gt=0)

    @model_validator(mode="after")
    def validate_training_window(self) -> WorkspaceForecastModelMetadata:
        """Keep the advertised recent-history window exactly sixty days."""
        if self.training_window_end - self.training_window_start != timedelta(days=59):
            raise ValueError("workspace forecast training window must be sixty days")
        return self


class WorkspaceForecastAvailable(_WorkspaceForecastContract):
    """A complete future path produced only after every trust gate passes."""

    status: Literal[WorkspaceForecastStatus.AVAILABLE] = (
        WorkspaceForecastStatus.AVAILABLE
    )
    workspace_id: Identifier
    workspace_revision: int = Field(ge=1)
    currency: Currency
    as_of_date: date
    horizon_days: WorkspaceForecastHorizon
    confirmed_balance: Money
    confirmed_balance_as_of: date
    expected_balance_as_of_today: Money
    model: WorkspaceForecastModelMetadata
    warnings: tuple[WorkspaceForecastWarningCode, ...] = Field(min_length=3)
    daily_balances: tuple[WorkspaceForecastPoint, ...] = Field(min_length=15)

    @model_validator(mode="after")
    def validate_path(self) -> WorkspaceForecastAvailable:
        """Require tomorrow-through-horizon points and mandatory caveats."""
        expected_dates = tuple(
            self.as_of_date + timedelta(days=offset)
            for offset in range(1, self.horizon_days + 1)
        )
        actual_dates = tuple(item.forecast_date for item in self.daily_balances)
        if actual_dates != expected_dates:
            raise ValueError(
                "workspace forecast must cover tomorrow through its horizon"
            )
        mandatory = {
            WorkspaceForecastWarningCode.ONE_SHOT_WORKSPACE_BASELINE,
            WorkspaceForecastWarningCode.EMPIRICAL_INTERVAL_ESTIMATE,
            WorkspaceForecastWarningCode.UNCLASSIFIED_TOTAL_CASH_FLOW,
        }
        if not mandatory.issubset(self.warnings):
            raise ValueError(
                "workspace forecasts must disclose baseline and interval limits"
            )
        return self


class WorkspaceForecastWithheld(_WorkspaceForecastContract):
    """Expected refusal carrying only controlled readiness reason codes."""

    status: Literal[WorkspaceForecastStatus.WITHHELD] = WorkspaceForecastStatus.WITHHELD
    workspace_id: Identifier
    workspace_revision: int = Field(ge=1)
    currency: Currency
    as_of_date: date
    horizon_days: WorkspaceForecastHorizon
    reasons: tuple[WorkspaceForecastReasonCode, ...] = Field(min_length=1)


WorkspaceForecastResult = Annotated[
    WorkspaceForecastAvailable | WorkspaceForecastWithheld,
    Field(discriminator="status"),
]


class WorkspaceForecastResponse(RootModel[WorkspaceForecastResult]):
    """HTTP/client wrapper that serialises as the discriminated result itself."""


__all__ = [
    "WorkspaceForecastAvailable",
    "WorkspaceForecastHorizon",
    "WorkspaceForecastIntervalMethod",
    "WorkspaceForecastModelMetadata",
    "WorkspaceForecastModelName",
    "WorkspaceForecastPoint",
    "WorkspaceForecastReasonCode",
    "WorkspaceForecastRequest",
    "WorkspaceForecastResponse",
    "WorkspaceForecastResult",
    "WorkspaceForecastStatus",
    "WorkspaceForecastWarningCode",
    "WorkspaceForecastWithheld",
]

"""Transport contracts for analytics, ML, and planning API orchestration."""

from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.financial_roles import TransactionReviewAction
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecast_paths import ForecastPathPlan
from cashflow_ai.schemas.forecasting import ForecastDatasetPlan
from cashflow_ai.schemas.freshness import FreshnessPolicy
from cashflow_ai.schemas.planning import PlanningEvaluationPlan
from cashflow_ai.schemas.recurrence import RecurrenceDetectionPolicy
from cashflow_ai.schemas.scenarios import FinancialScenario
from cashflow_ai.schemas.transactions import Identifier


class _DecisionApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FinancialDataFreshnessRequest(_DecisionApiContract):
    """Account, date, and explicit evidence-age policy for one assessment."""

    account_id: Identifier
    as_of_date: date
    policy: FreshnessPolicy


class RecurrenceDetectionRequest(_DecisionApiContract):
    """Owned profile and point-in-time policy for one recurrence refresh."""

    user_profile_id: Identifier
    as_of_date: date
    knowledge_cutoff_at: AwareDatetime
    policy: RecurrenceDetectionPolicy


class FinancialRoleSuggestionRequest(_DecisionApiContract):
    """Profile scope for server-timestamped advisory suggestion generation."""

    user_profile_id: Identifier


class RoleDecisionRequest(_DecisionApiContract):
    """Aware caller observation time for confirming or rejecting a suggestion."""

    reviewed_at: AwareDatetime


class TransactionRoleReviewRequest(_DecisionApiContract):
    """Explicit user role action and aware caller observation time."""

    action: TransactionReviewAction
    changed_at: AwareDatetime


class ForecastEvaluationRequest(_DecisionApiContract):
    """Leakage-safe dataset plan and explicit candidate-selection policy."""

    dataset_plan: ForecastDatasetPlan
    model_policy: ForecastModelPolicy


class BalanceForecastRequest(ForecastEvaluationRequest):
    """Complete server-side inputs for one future daily balance path."""

    path_plan: ForecastPathPlan

    @model_validator(mode="after")
    def validate_alignment(self) -> BalanceForecastRequest:
        """Require one account, profile, and cutoff across the forecast pipeline."""
        if (
            self.dataset_plan.user_profile_id != self.path_plan.user_profile_id
            or self.dataset_plan.account_ids != (self.path_plan.account_id,)
            or self.dataset_plan.knowledge_cutoff_at
            != self.path_plan.knowledge_cutoff_at
        ):
            raise ValueError(
                "forecast dataset and path must share one profile, account, and cutoff"
            )
        return self


class PlanningApiRequest(_DecisionApiContract):
    """Planning scope plus one server-computed forecast per selected account."""

    plan: PlanningEvaluationPlan
    forecasts: tuple[BalanceForecastRequest, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_scope(self) -> PlanningApiRequest:
        """Align forecast order and ownership with the planning account scope."""
        account_ids = tuple(item.path_plan.account_id for item in self.forecasts)
        if account_ids != self.plan.account_ids or any(
            item.path_plan.user_profile_id != self.plan.user_profile_id
            for item in self.forecasts
        ):
            raise ValueError(
                "planning forecasts must match the ordered profile account scope"
            )
        if any(
            item.path_plan.forecast_start <= self.plan.as_of_date
            for item in self.forecasts
        ):
            raise ValueError("planning forecasts must start after the as-of date")
        return self


class ScenarioApiRequest(_DecisionApiContract):
    """One isolated scenario evaluated from server-rebuilt forecast evidence."""

    forecast: BalanceForecastRequest
    planning_plan: PlanningEvaluationPlan
    scenario: FinancialScenario

    @model_validator(mode="after")
    def validate_scope(self) -> ScenarioApiRequest:
        """Keep scenario, forecasting, and planning ownership aligned."""
        profile_id = self.forecast.path_plan.user_profile_id
        account_id = self.forecast.path_plan.account_id
        if (
            self.planning_plan.user_profile_id != profile_id
            or self.planning_plan.account_ids != (account_id,)
            or self.scenario.user_profile_id != profile_id
            or self.scenario.account_id != account_id
        ):
            raise ValueError(
                "scenario, forecast, and planning scopes must use one profile account"
            )
        return self


__all__ = [
    "BalanceForecastRequest",
    "FinancialDataFreshnessRequest",
    "FinancialRoleSuggestionRequest",
    "ForecastEvaluationRequest",
    "PlanningApiRequest",
    "RecurrenceDetectionRequest",
    "RoleDecisionRequest",
    "ScenarioApiRequest",
    "TransactionRoleReviewRequest",
]

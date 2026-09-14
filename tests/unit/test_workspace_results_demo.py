"""Tests for the synthetic workspace-results manual walkthrough."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import cashflow_ai.workspaces.results_demo as demo
from cashflow_ai.schemas.workspace_analytics import WorkspaceAnalyticsRequest
from cashflow_ai.schemas.workspace_forecasts import WorkspaceForecastRequest
from cashflow_ai.workspaces.analytics import compute_workspace_analytics
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace


def test_workspace_results_demo_is_readable_and_creates_no_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo.main()

    output = capsys.readouterr().out
    assert "CashFlow AI synthetic workspace-results check" in output
    assert "coverage: complete (90/90 days)" in output
    assert "income: £4,500.00" in output
    assert "spending: £2,760.00" in output
    assert "net transfers: -£200.00" in output
    assert "15-day forecast: available" in output
    assert "30-day forecast: available" in output
    assert "unresolved-role guard: withheld (unknown_financial_roles)" in output
    assert "files, databases, and real financial data created: no" in output


def test_workspace_results_demo_defensive_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = demo._synthetic_workspace()
    analytics = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )
    no_totals = analytics.model_copy(update={"totals": None})
    monkeypatch.setattr(
        demo, "compute_workspace_analytics", MagicMock(return_value=no_totals)
    )
    with pytest.raises(RuntimeError, match="coverage unexpectedly"):
        demo.main()

    monkeypatch.setattr(
        demo, "compute_workspace_analytics", compute_workspace_analytics
    )
    monkeypatch.setattr(
        demo,
        "_synthetic_workspace",
        MagicMock(return_value=workspace.model_copy(update={"balance": None})),
    )
    with pytest.raises(RuntimeError, match="balance unexpectedly"):
        demo.main()

    monkeypatch.setattr(demo, "_synthetic_workspace", MagicMock(return_value=workspace))
    withheld = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=workspace.revision - 1,
            horizon_days=15,
        ),
        today=demo._TODAY,
    )
    monkeypatch.setattr(
        demo,
        "forecast_statement_workspace",
        MagicMock(return_value=withheld),
    )
    with pytest.raises(RuntimeError, match="history unexpectedly withheld"):
        demo.main()

    available = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=workspace.revision,
            horizon_days=15,
        ),
        today=demo._TODAY,
    )
    monkeypatch.setattr(
        demo,
        "forecast_statement_workspace",
        MagicMock(return_value=available),
    )
    with pytest.raises(RuntimeError, match="unresolved role unexpectedly"):
        demo.main()

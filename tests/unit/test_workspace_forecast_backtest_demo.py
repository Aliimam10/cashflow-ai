from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import cashflow_ai.workspaces.forecast_backtest_demo as demo
from cashflow_ai.imports.csv_preview import preview_csv
from cashflow_ai.schemas.transactions import Currency
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastReasonCode,
    WorkspaceForecastRequest,
    WorkspaceForecastResult,
    WorkspaceForecastWithheld,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceRetentionMode,
    WorkspaceStatus,
)
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace
from cashflow_ai.workspaces.service import (
    finalize_workspace as service_finalize_workspace,
)
from cashflow_ai.workspaces.service import (
    review_workspace_uploads as service_review_workspace_uploads,
)


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_backtest_uses_real_workspace_adapter_before_revealing_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    captured_workspaces: list[StatementWorkspace] = []
    real_write = demo._write_consolidated_statement
    real_review = service_review_workspace_uploads
    real_finalize = service_finalize_workspace
    real_score = demo._score_forecast

    def recording_write(
        path: Path, rows: tuple[demo.SyntheticStatementRow, ...]
    ) -> None:
        events.append(f"write:{path.name}")
        real_write(path, rows)

    def recording_review(*args: Any, **kwargs: Any) -> Any:
        events.append("review")
        return real_review(*args, **kwargs)

    def recording_finalize(*args: Any, **kwargs: Any) -> Any:
        events.append("finalize")
        return real_finalize(*args, **kwargs)

    def recording_forecast(
        workspace: StatementWorkspace,
        request: WorkspaceForecastRequest,
        *,
        today: date | None = None,
    ) -> WorkspaceForecastResult:
        events.append("forecast")
        captured_workspaces.append(workspace)
        return forecast_statement_workspace(workspace, request, today=today)

    def recording_score(
        forecast: WorkspaceForecastAvailable,
        evaluation_rows: tuple[demo.SyntheticStatementRow, ...],
    ) -> demo.ForecastBacktestMetrics:
        events.append("score")
        return real_score(forecast, evaluation_rows)

    monkeypatch.setattr(demo, "_write_consolidated_statement", recording_write)
    monkeypatch.setattr(demo, "review_workspace_uploads", recording_review)
    monkeypatch.setattr(demo, "finalize_workspace", recording_finalize)
    monkeypatch.setattr(demo, "forecast_statement_workspace", recording_forecast)
    monkeypatch.setattr(demo, "_score_forecast", recording_score)
    run = demo.run_backtest(tmp_path)

    assert events == [
        f"write:{demo._TRAINING_FILENAME}",
        "review",
        "finalize",
        "forecast",
        f"write:{demo._EVALUATION_FILENAME}",
        f"write:{demo._MASTER_FILENAME}",
        "score",
    ]

    training_preview = preview_csv(
        run.training_path.read_bytes(),
        run.training_path.name,
        preview_rows=100,
    )
    evaluation_preview = preview_csv(
        run.evaluation_path.read_bytes(),
        run.evaluation_path.name,
        preview_rows=100,
    )
    master_preview = preview_csv(
        run.master_path.read_bytes(),
        run.master_path.name,
        preview_rows=100,
    )
    assert training_preview.total_data_rows == 60
    assert evaluation_preview.total_data_rows == 30
    assert master_preview.total_data_rows == 90
    assert training_preview.parser_name == "revolut_consolidated_csv"
    assert training_preview.layout_version == "consolidated_v2_gbp_1"
    assert training_preview.warning_codes == ("non_gbp_transaction_sections_excluded",)
    assert training_preview.excluded_transaction_rows == 1
    assert training_preview.suggestions.transaction_date == ("Date",)
    assert training_preview.suggestions.description == ("Description",)
    assert training_preview.suggestions.signed_amount == ("Money in/out",)
    assert training_preview.suggestions.running_balance == ("Balance",)
    assert training_preview.rows[0].values[0] == "Jun 17, 2026"
    assert training_preview.rows[-1].values[0] == "Aug 15, 2026"
    assert evaluation_preview.rows[0].values[0] == "Aug 16, 2026"
    assert evaluation_preview.rows[-1].values[0] == "Sep 14, 2026"
    assert all(
        row.values[1].startswith("FICTIONAL ")
        for preview in (training_preview, evaluation_preview, master_preview)
        for row in preview.rows
    )
    assert list(demo._DUAL_CURRENCY_HEADERS) in _read_rows(run.training_path)

    assert len(captured_workspaces) == 1
    workspace = captured_workspaces[0]
    assert workspace.status is WorkspaceStatus.FINALIZED
    assert workspace.retention_mode is WorkspaceRetentionMode.TEMPORARY
    assert workspace.sources == ()
    assert len(workspace.rows) == 60
    assert all(row.source_id is None for row in workspace.rows)
    assert (
        max(
            row.transaction_date
            for row in workspace.rows
            if row.transaction_date is not None
        )
        == run.training_end
    )
    assert run.evaluation_start == run.training_end + timedelta(days=1)
    assert run.forecast.model.training_window_end == run.training_end
    assert run.forecast.model.training_window_start == run.training_start
    assert run.parser_name == "revolut_consolidated_csv"
    assert run.layout_version == "consolidated_v2_gbp_1"
    assert run.excluded_non_gbp_rows == 1
    assert run.source_exclusions_confirmed is True


def test_backtest_reports_reproducible_holdout_metrics(tmp_path: Path) -> None:
    first = demo.run_backtest(tmp_path / "first")
    second = demo.run_backtest(tmp_path / "second")

    assert first.forecast.daily_balances == second.forecast.daily_balances
    assert first.metrics == second.metrics
    assert first.metrics.evaluated_days == 30
    assert first.metrics.interval_days_covered == 28
    assert first.metrics.interval_coverage_percent == Decimal("93.33")
    assert first.metrics.daily_mae == Decimal("9.39")
    assert first.metrics.daily_rmse == Decimal("11.93")
    assert first.metrics.final_expected_balance == Decimal("2839.56")
    assert first.metrics.final_actual_balance == Decimal("2848.00")
    assert first.metrics.final_error == Decimal("-8.44")
    assert first.metrics.final_actual_in_interval is True


def test_main_prints_clear_comparison_and_false_interval_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    demo.main()

    output = capsys.readouterr().out
    assert "CashFlow AI synthetic 60/30 forecast backtest" in output
    assert "master 90-day CSV:" in output
    assert "training window: 2026-06-17 to 2026-08-15 (60 days)" in output
    assert "hidden evaluation: 2026-08-16 to 2026-09-14 (30 days)" in output
    assert "30 later rows were revealed only for scoring" in output
    assert "adapter: revolut_consolidated_csv / consolidated_v2_gbp_1" in output
    assert "non-GBP exclusion review: confirmed (1 fictional row excluded)" in output
    assert "model fit: recent_60_day_weekday_mean" in output
    assert "30-day expected closing balance: £2,839.56" in output
    assert "30-day actual closing balance: £2,848.00" in output
    assert "final balance error (forecast - actual): -£8.44" in output
    assert "daily MAE: £9.39" in output
    assert "daily RMSE: £11.93" in output
    assert "80% interval coverage: 93.33% (28/30 days)" in output
    assert "actual final balance inside 80% interval: yes" in output
    assert "fictional data only; generated CSVs are Git-ignored" in output

    run = demo.run_backtest(tmp_path / "forced-outcome")
    false_metrics = replace(run.metrics, final_actual_in_interval=False)
    monkeypatch.setattr(
        demo,
        "run_backtest",
        MagicMock(return_value=replace(run, metrics=false_metrics)),
    )
    demo.main()
    assert "actual final balance inside 80% interval: no" in capsys.readouterr().out


def test_backtest_fails_closed_when_forecast_is_withheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    withheld = WorkspaceForecastWithheld(
        workspace_id="fictional-forecast-backtest",
        workspace_revision=1,
        currency=Currency.GBP,
        as_of_date=demo._START_DATE + timedelta(days=59),
        horizon_days=30,
        reasons=(WorkspaceForecastReasonCode.INSUFFICIENT_HISTORY,),
    )
    monkeypatch.setattr(
        demo,
        "forecast_statement_workspace",
        MagicMock(return_value=withheld),
    )

    with pytest.raises(RuntimeError, match="unexpectedly withheld"):
        demo.run_backtest(tmp_path)

    assert (tmp_path / demo._TRAINING_FILENAME).is_file()
    assert not (tmp_path / demo._EVALUATION_FILENAME).exists()
    assert not (tmp_path / demo._MASTER_FILENAME).exists()


def test_backtest_fails_closed_without_expected_adapter_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_review = service_review_workspace_uploads

    def altered_review(*args: Any, **kwargs: Any) -> Any:
        review = real_review(*args, **kwargs)
        source = review.workspace.sources[0].model_copy(
            update={"parser_name": "generic_csv"}
        )
        workspace = review.workspace.model_copy(update={"sources": (source,)})
        return review.model_copy(update={"workspace": workspace})

    monkeypatch.setattr(demo, "review_workspace_uploads", altered_review)

    with pytest.raises(RuntimeError, match="adapter evidence is incomplete"):
        demo.run_backtest(tmp_path)

    assert (tmp_path / demo._TRAINING_FILENAME).is_file()
    assert not (tmp_path / demo._EVALUATION_FILENAME).exists()
    assert not (tmp_path / demo._MASTER_FILENAME).exists()

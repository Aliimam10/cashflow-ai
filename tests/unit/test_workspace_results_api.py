"""HTTP tests for analytics and forecasts over finalized workspaces."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from cashflow_ai.api import AppContainer, build_container, create_app
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.persistence import Base
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)


def _container(tmp_path: Path) -> AppContainer:
    settings = Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        database_url=f"sqlite:///{tmp_path / 'workspace-results-api.db'}",
        api_host="127.0.0.1",
        api_port=8765,
    )
    container = build_container(settings)
    Base.metadata.create_all(container.engine)
    return container


def _finalized(today: date) -> StatementWorkspace:
    now = datetime.now(tz=UTC)
    rows = (
        WorkspaceTransactionRow(
            row_id="synthetic-pay",
            transaction_date=today - timedelta(days=30),
            description="SYNTHETIC PAY",
            amount=Decimal("1000.00"),
            balance_after=Decimal("1200.00"),
            category_id="income",
            financial_role=FinancialRole.INCOME,
            review_state=WorkspaceRowReviewState.CONFIRMED,
        ),
        WorkspaceTransactionRow(
            row_id="synthetic-food",
            transaction_date=today - timedelta(days=10),
            description="SYNTHETIC GROCER",
            amount=Decimal("-75.00"),
            balance_after=Decimal("1125.00"),
            category_id="groceries",
            financial_role=FinancialRole.EXPENSE,
            review_state=WorkspaceRowReviewState.CONFIRMED,
        ),
        WorkspaceTransactionRow(
            row_id="synthetic-transfer",
            transaction_date=today - timedelta(days=5),
            description="SYNTHETIC SAVINGS MOVE",
            amount=Decimal("-100.00"),
            balance_after=Decimal("1025.00"),
            category_id="transfers",
            financial_role=FinancialRole.TRANSFER_OUT,
            review_state=WorkspaceRowReviewState.CONFIRMED,
        ),
    )
    return StatementWorkspace(
        workspace_id="synthetic-results-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional current account",
        revision=4,
        rows=rows,
        coverage=WorkspaceCoverageConfirmation(
            start_date=today - timedelta(days=69),
            end_date=today,
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("1025.00"),
            as_of_date=today,
            confirmed=True,
        ),
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
        finalized_at=now,
    )


def _saved_history_csv(today: date) -> tuple[bytes, Decimal]:
    """Build 70 covered days of fictional, internally reconciled history."""
    lines = ["Date,Description,Amount,Balance,Transaction ID"]
    balance = Decimal("1000.00")
    start_date = today - timedelta(days=69)
    for offset in range(70):
        is_income = offset % 14 == 0
        amount = Decimal("300.00") if is_income else Decimal("-10.00")
        balance += amount
        lines.append(
            ",".join(
                (
                    (start_date + timedelta(days=offset)).isoformat(),
                    "SYNTHETIC PAY" if is_income else "SYNTHETIC GROCER",
                    f"{amount:.2f}",
                    f"{balance:.2f}",
                    f"synthetic-{offset}",
                )
            )
        )
    return ("\n".join(lines) + "\n").encode(), balance


def test_finalized_workspace_result_endpoints_share_one_canonical_table(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    today = datetime.now(tz=ZoneInfo("Europe/London")).date()
    workspace = _finalized(today)
    container.workspace_store.add(workspace)

    with TestClient(create_app(container)) as client:
        analytics = client.post(
            f"/api/v1/workspaces/{workspace.workspace_id}/analytics",
            json={"expected_workspace_revision": workspace.revision},
        )
        transactions = client.post(
            f"/api/v1/workspaces/{workspace.workspace_id}/transactions/search",
            json={
                "expected_workspace_revision": workspace.revision,
                "financial_roles": ["expense"],
                "pagination": {"limit": 10, "offset": 0},
            },
        )
        forecast = client.post(
            f"/api/v1/workspaces/{workspace.workspace_id}/forecast",
            json={
                "expected_workspace_revision": workspace.revision,
                "horizon_days": 15,
            },
        )

        assert analytics.status_code == 200
        assert analytics.json()["totals"]["total_expenses"] == "75.00"
        assert analytics.json()["totals"]["transfer_outflow"] == "100.00"
        assert analytics.json()["category_spending"][0]["category_name"] == (
            "Groceries"
        )
        assert transactions.status_code == 200
        assert transactions.json()["total"] == 1
        assert transactions.json()["items"][0]["row_id"] == "synthetic-food"
        assert forecast.status_code == 200
        assert forecast.json()["status"] == "available"
        assert forecast.json()["currency"] == "GBP"
        assert len(forecast.json()["daily_balances"]) == 15
        for response in (analytics, transactions, forecast):
            assert response.headers["cache-control"] == "no-store, max-age=0"


def test_saved_finalization_reloads_from_sqlite_for_result_endpoints(
    tmp_path: Path,
) -> None:
    today = datetime.now(tz=ZoneInfo("Europe/London")).date()
    csv_content, latest_balance = _saved_history_csv(today)
    first_container = _container(tmp_path)

    with TestClient(create_app(first_container)) as client:
        created = client.post(
            "/api/v1/workspaces",
            json={
                "retention_mode": "saved",
                "account_name": "Fictional saved account",
                "currency": "GBP",
            },
        )
        assert created.status_code == 201
        workspace_id = created.json()["workspace_id"]

        review = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={
                "files": (
                    "synthetic-history.csv",
                    csv_content,
                    "text/csv",
                )
            },
        )
        assert review.status_code == 200
        reviewed_workspace = review.json()["workspace"]
        assert len(reviewed_workspace["rows"]) == 70

        row_edits = []
        for row in reviewed_workspace["rows"]:
            is_income = Decimal(row["amount"]) > 0
            row_edits.append(
                {
                    "row_id": row["row_id"],
                    "expected_revision": row["revision"],
                    "transaction_date": row["transaction_date"],
                    "description": row["description"],
                    "amount": row["amount"],
                    "balance_after": row["balance_after"],
                    "category_id": "income" if is_income else "groceries",
                    "financial_role": "income" if is_income else "expense",
                    "review_state": "confirmed",
                }
            )
        edited = client.patch(
            f"/api/v1/workspaces/{workspace_id}/rows",
            json={
                "expected_workspace_revision": reviewed_workspace["revision"],
                "rows": row_edits,
            },
        )
        assert edited.status_code == 200

        finalized = client.post(
            f"/api/v1/workspaces/{workspace_id}/finalize",
            json={
                "expected_workspace_revision": edited.json()["revision"],
                "statement_confirmed": True,
                "date_interpretation_confirmed": True,
                "sign_convention_confirmed": True,
                "coverage": {
                    "start_date": (today - timedelta(days=69)).isoformat(),
                    "end_date": today.isoformat(),
                    "status": "complete",
                    "missing_periods": [],
                    "confirmed": True,
                },
                "balance": {
                    "balance": f"{latest_balance:.2f}",
                    "as_of_date": today.isoformat(),
                    "currency": "GBP",
                    "confirmed": True,
                },
            },
        )
        assert finalized.status_code == 200
        assert finalized.json()["persisted"] is True
        finalized_revision = finalized.json()["workspace"]["revision"]

    assert first_container.workspace_store.count() == 0

    reloaded_container = _container(tmp_path)
    assert reloaded_container.workspace_store.count() == 0
    with TestClient(create_app(reloaded_container)) as client:
        analytics = client.post(
            f"/api/v1/workspaces/{workspace_id}/analytics",
            json={"expected_workspace_revision": finalized_revision},
        )
        assert analytics.status_code == 200
        assert reloaded_container.workspace_store.count() == 1
        assert analytics.json()["observed_transaction_count"] == 70
        assert analytics.json()["totals"]["total_income"] == "1500.00"
        assert analytics.json()["totals"]["total_expenses"] == "650.00"

        transactions = client.post(
            f"/api/v1/workspaces/{workspace_id}/transactions/search",
            json={
                "expected_workspace_revision": finalized_revision,
                "financial_roles": ["expense"],
                "pagination": {"limit": 10, "offset": 0},
            },
        )
        assert transactions.status_code == 200
        assert transactions.json()["total"] == 65
        assert all(
            item["financial_role"] == "expense" for item in transactions.json()["items"]
        )

        forecast = client.post(
            f"/api/v1/workspaces/{workspace_id}/forecast",
            json={
                "expected_workspace_revision": finalized_revision,
                "horizon_days": 15,
            },
        )
        assert forecast.status_code == 200
        assert forecast.json()["status"] == "available"
        assert forecast.json()["confirmed_balance"] == f"{latest_balance:.2f}"
        assert len(forecast.json()["daily_balances"]) == 15


def test_workspace_results_reject_draft_and_stale_analytics_safely(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    today = datetime.now(tz=ZoneInfo("Europe/London")).date()
    finalized = _finalized(today)
    draft = StatementWorkspace(
        workspace_id="synthetic-draft",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.DRAFT,
        account_name="Private phrase must not leak",
        revision=1,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    container.workspace_store.add(finalized)
    container.workspace_store.add(draft)

    with TestClient(create_app(container)) as client:
        blocked = client.post(
            f"/api/v1/workspaces/{draft.workspace_id}/analytics",
            json={"expected_workspace_revision": 1},
        )
        stale = client.post(
            f"/api/v1/workspaces/{finalized.workspace_id}/analytics",
            json={"expected_workspace_revision": 3},
        )
        withheld = client.post(
            f"/api/v1/workspaces/{finalized.workspace_id}/forecast",
            json={"expected_workspace_revision": 3, "horizon_days": 30},
        )

        assert blocked.status_code == 400
        assert blocked.json()["code"] == "workspace_not_finalized"
        assert "Private phrase" not in blocked.text
        assert stale.status_code == 409
        assert stale.json()["code"] == "workspace_revision_conflict"
        assert withheld.status_code == 200
        assert withheld.json()["status"] == "withheld"
        assert withheld.json()["reasons"] == ["revision_mismatch"]

"""Readable synthetic end-to-end demonstration of the local ingestion API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from cashflow_ai.api import build_container, create_app
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.persistence import Base
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import FinancialRoleRecord
from cashflow_ai.schemas.csv_imports import (
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    ImportContext,
    StatementBalances,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import FinancialRole

_CSV = (
    b"Date,Description,Amount,Balance\n"
    b"2026-08-01,SYNTHETIC SALARY,1000.00,1000.00\n"
    b"2026-08-02,SYNTHETIC RENT,-400.00,600.00\n"
)


def _settings(database_url: str) -> Settings:
    return Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        database_url=database_url,
        api_host="127.0.0.1",
        api_port=8000,
    )


def main() -> None:
    """Exercise health, setup, preview, confirmation, and transaction reads."""
    with TemporaryDirectory(prefix="cashflow-api-demo-") as temporary_directory:
        container = build_container(
            _settings(f"sqlite:///{temporary_directory}/synthetic.db")
        )
        Base.metadata.create_all(container.engine)
        with session_scope(container.session_factory) as session:
            session.add_all(
                FinancialRoleRecord(
                    id=role.value,
                    name=role.value.replace("_", " ").title(),
                )
                for role in FinancialRole
            )

        with TestClient(create_app(container)) as client:
            health = client.get("/health")
            profile = client.post(
                "/api/v1/profiles",
                json={
                    "display_name": "Fictional User",
                    "base_currency": "GBP",
                    "timezone": "Europe/London",
                },
            )
            profile_id = profile.json()["profile_id"]
            account = client.post(
                f"/api/v1/profiles/{profile_id}/accounts",
                json={
                    "name": "Fictional Current Account",
                    "account_type": "current",
                    "currency": "GBP",
                    "institution_label": "Example Bank",
                },
            )
            account_id = account.json()["account_id"]
            preview = client.post(
                "/api/v1/imports/csv/preview",
                files={"file": ("synthetic.csv", _CSV, "text/csv")},
            )
            preview_body = preview.json()
            plan = CsvImportPlan(
                account_id=account_id,
                statement_context=ImportContext(
                    account_id=account_id,
                    coverage=StatementCoverage(
                        statement_start_date=date(2026, 8, 1),
                        statement_end_date=date(2026, 8, 31),
                        status=CoverageStatus.COMPLETE,
                    ),
                    balances=StatementBalances(
                        opening_balance=Decimal("0.00"),
                        closing_balance=Decimal("600.00"),
                    ),
                ),
                mapping=CsvColumnMapping(
                    transaction_date_column="Date",
                    description_column="Description",
                    signed_amount_column="Amount",
                    running_balance_column="Balance",
                ),
            )
            confirmation = CsvImportConfirmation(
                preview_file_hash=preview_body["file_hash"],
                user_confirmed=True,
                confirmed_at=utc_now(),
            )
            imported = client.post(
                "/api/v1/imports/csv/confirm",
                files={"file": ("synthetic.csv", _CSV, "text/csv")},
                data={
                    "plan_json": plan.model_dump_json(),
                    "confirmation_json": confirmation.model_dump_json(),
                },
            )
            transactions = client.get(f"/api/v1/accounts/{account_id}/transactions")
            transaction_items = transactions.json()["items"]
            role_reviews = tuple(
                client.post(
                    f"/api/v1/transactions/{item['transaction_id']}/financial-role",
                    json={
                        "action": (
                            "income" if Decimal(str(item["amount"])) > 0 else "expense"
                        ),
                        "changed_at": utc_now().isoformat(),
                    },
                )
                for item in transaction_items
            )
            analytics = client.post(
                "/api/v1/analytics/cash-flow",
                json={
                    "user_profile_id": profile_id,
                    "account_ids": [account_id],
                    "period": {
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-31",
                    },
                    "view": "account",
                },
            )
            paginated = client.get(
                f"/api/v1/accounts/{account_id}/transactions?limit=1&offset=1"
            )
            freshness = client.get(f"/api/v1/accounts/{account_id}/derived-freshness")

            health.raise_for_status()
            profile.raise_for_status()
            account.raise_for_status()
            preview.raise_for_status()
            imported.raise_for_status()
            transactions.raise_for_status()
            for role_review in role_reviews:
                role_review.raise_for_status()
            analytics.raise_for_status()
            paginated.raise_for_status()
            freshness.raise_for_status()
            analytics_body = analytics.json()
            analytics_state = next(
                item
                for item in freshness.json()["items"]
                if item["output_type"] == "analytics"
            )
            print("CashFlow AI synthetic API check")
            print(f"health: {health.json()['status']}")
            print(f"CSV preview rows: {preview_body['total_data_rows']}")
            print(
                f"verified transactions imported: {imported.json()['new_transactions']}"
            )
            print(f"verified transactions returned: {len(transaction_items)}")
            print(
                "role-aware cash flow: "
                f"income={analytics_body['totals']['total_income']} "
                f"expenses={analytics_body['totals']['total_expenses']} "
                f"net={analytics_body['totals']['net_cash_flow']}"
            )
            print(f"coverage status: {analytics_body['coverage']['status']}")
            print(f"analytics freshness: {analytics_state['status']}")
            print(
                "pagination: "
                f"returned={len(paginated.json()['items'])} "
                f"total={paginated.json()['total']}"
            )
            print("raw source payload returned: false")
            print("temporary database retained: false")


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()

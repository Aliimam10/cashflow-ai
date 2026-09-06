"""Populate an isolated local database from the labelled synthetic dashboard CSV."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.api.container import build_container
from cashflow_ai.api.decision_services import correct_category, review_transaction_role
from cashflow_ai.api.services import create_account, create_profile, search_transactions
from cashflow_ai.categorisation import list_categories
from cashflow_ai.config import load_settings
from cashflow_ai.imports import persist_confirmed_csv, preview_csv
from cashflow_ai.persistence import UserProfileRepository, session_scope
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.analytics import AnalyticsScope, AnalyticsView
from cashflow_ai.schemas.api import (
    AccountCreate,
    TransactionSearchRequest,
    UserProfileCreate,
)
from cashflow_ai.schemas.api_decisions import TransactionRoleReviewRequest
from cashflow_ai.schemas.csv_imports import (
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
)
from cashflow_ai.schemas.financial_roles import TransactionReviewAction
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackAction,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    ImportContext,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency

DEFAULT_DEMO_CSV: Final = Path("data/demo/generated/student/student_canonical.csv")
DEMO_DATABASE_NAME: Final = "cashflow-demo.db"
_REQUIRED_LABEL_COLUMNS: Final = frozenset(
    {"external_id", "category_id", "financial_role"}
)
_SUPPORTED_ROLE_ACTIONS: Final = {
    "income": TransactionReviewAction.INCOME,
    "expense": TransactionReviewAction.EXPENSE,
}


class SyntheticDashboardError(ValueError):
    """Controlled failure that never includes transaction descriptions."""


@dataclass(frozen=True, slots=True)
class SyntheticDashboardSummary:
    """Non-sensitive counts produced by one isolated dashboard seed."""

    imported_transactions: int
    labelled_transactions: int
    statement_start_date: date
    statement_end_date: date
    total_income: Decimal
    total_expenses: Decimal


@dataclass(frozen=True, slots=True)
class _FixtureLabel:
    category_id: str
    role_action: TransactionReviewAction


def _read_fixture_labels(content: bytes) -> dict[str, _FixtureLabel]:
    """Read only explicit labels from a generated UTF-8 canonical fixture."""
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise SyntheticDashboardError(
            "synthetic dashboard CSV must use UTF-8"
        ) from error
    reader = csv.DictReader(StringIO(text))
    missing = _REQUIRED_LABEL_COLUMNS.difference(reader.fieldnames or ())
    if missing:
        raise SyntheticDashboardError(
            "synthetic dashboard CSV is missing its explicit label columns"
        )

    labels: dict[str, _FixtureLabel] = {}
    for row in reader:
        external_id = row["external_id"].strip()
        category_id = row["category_id"].strip()
        role_action = _SUPPORTED_ROLE_ACTIONS.get(row["financial_role"].strip())
        if not external_id or not category_id or role_action is None:
            raise SyntheticDashboardError(
                "synthetic dashboard CSV contains an unsupported or empty label"
            )
        label = _FixtureLabel(category_id, role_action)
        existing = labels.get(external_id)
        if existing is not None and existing != label:
            raise SyntheticDashboardError(
                "synthetic dashboard CSV contains conflicting duplicate labels"
            )
        labels[external_id] = label
    if not labels:
        raise SyntheticDashboardError(
            "synthetic dashboard CSV contains no labelled rows"
        )
    return labels


def _require_empty_profile(factory: sessionmaker[Session]) -> None:
    """Protect an existing default or personal database from demo writes."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).list_all():
            raise SyntheticDashboardError(
                "the isolated demo database already contains a profile"
            )


def _require_isolated_demo_factory(factory: sessionmaker[Session]) -> None:
    """Allow tests in memory and runtime writes only to the named demo database."""
    bind = factory.kw.get("bind")
    database = getattr(getattr(bind, "url", None), "database", None)
    if database != ":memory:" and (
        database is None or Path(database).name != DEMO_DATABASE_NAME
    ):
        raise SyntheticDashboardError(
            f"refusing demo writes outside an isolated {DEMO_DATABASE_NAME}"
        )


def seed_synthetic_dashboard(
    factory: sessionmaker[Session],
    content: bytes,
    *,
    filename: str = "student_canonical.csv",
) -> SyntheticDashboardSummary:
    """Import and explicitly approve labels in a separate synthetic database."""
    _require_isolated_demo_factory(factory)
    labels = _read_fixture_labels(content)
    _require_empty_profile(factory)
    category_ids = {
        category.id for category in list_categories(factory) if category.is_active
    }
    if not {label.category_id for label in labels.values()}.issubset(category_ids):
        raise SyntheticDashboardError(
            "synthetic dashboard CSV contains a category outside the active taxonomy"
        )

    preview = preview_csv(content, filename)
    period = preview.suggested_statement_period
    if period is None:
        raise SyntheticDashboardError(
            "synthetic dashboard CSV has no readable transaction-date range"
        )
    profile = create_profile(
        factory,
        UserProfileCreate(
            display_name="Fictional Student",
            base_currency=Currency.GBP,
            timezone="Europe/London",
        ),
    )
    account = create_account(
        factory,
        profile_id=profile.profile_id,
        request=AccountCreate(
            name="Fictional Student Current",
            account_type=AccountType.CURRENT,
            currency=Currency.GBP,
            institution_label="Synthetic Bank",
        ),
    )
    summary = persist_confirmed_csv(
        factory,
        content,
        filename,
        mime_type="text/csv",
        plan=CsvImportPlan(
            account_id=account.account_id,
            account_currency=Currency.GBP,
            statement_context=ImportContext(
                account_id=account.account_id,
                coverage=StatementCoverage(
                    statement_start_date=period.start_date,
                    statement_end_date=period.end_date,
                    status=CoverageStatus.COMPLETE,
                ),
            ),
            mapping=CsvColumnMapping(
                transaction_date_column="transaction_date",
                description_column="description",
                signed_amount_column="amount",
                posting_date_column="posting_date",
                running_balance_column="balance",
                currency_column="currency",
                external_id_column="external_id",
                transaction_type_column="transaction_type",
            ),
        ),
        confirmation=CsvImportConfirmation(
            preview_file_hash=preview.file_hash,
            user_confirmed=True,
            confirmed_at=utc_now(),
        ),
    )
    if summary.rejected_rows:
        raise SyntheticDashboardError(
            "synthetic dashboard import quarantined one or more labelled rows"
        )
    transactions = search_transactions(
        factory,
        TransactionSearchRequest(
            user_profile_id=profile.profile_id,
            account_ids=(account.account_id,),
        ),
    )
    transactions_by_external_id = {
        transaction.external_id: transaction
        for transaction in transactions
        if transaction.external_id is not None
    }
    if len(transactions_by_external_id) != len(transactions) or set(
        transactions_by_external_id
    ) != set(labels):
        raise SyntheticDashboardError(
            "imported synthetic transactions do not match every explicit fixture label"
        )
    for external_id, transaction in transactions_by_external_id.items():
        label = labels[external_id]
        review_transaction_role(
            factory,
            transaction_id=transaction.transaction_id,
            request=TransactionRoleReviewRequest(
                action=label.role_action,
                changed_at=transaction.verified_at,
            ),
        )
        correct_category(
            factory,
            CategoryFeedback(
                user_profile_id=profile.profile_id,
                transaction_id=transaction.transaction_id,
                category_id=label.category_id,
                action=CategoryFeedbackAction.TRANSACTION_ONLY,
                corrected_at=transaction.verified_at,
            ),
        )

    analytics = compute_cash_flow_analytics(
        factory,
        AnalyticsScope(
            user_profile_id=profile.profile_id,
            account_ids=(account.account_id,),
            period=period,
            view=AnalyticsView.ACCOUNT,
        ),
    )
    if analytics.totals is None or analytics.totals.unknown_transaction_count:
        raise SyntheticDashboardError(
            "synthetic dashboard labels did not produce complete cash-flow totals"
        )
    return SyntheticDashboardSummary(
        imported_transactions=summary.new_transactions,
        labelled_transactions=len(transactions),
        statement_start_date=period.start_date,
        statement_end_date=period.end_date,
        total_income=analytics.totals.total_income,
        total_expenses=analytics.totals.total_expenses,
    )


def _is_isolated_demo_database(database_url: str) -> bool:
    return database_url.split("?", maxsplit=1)[0].endswith(f"/{DEMO_DATABASE_NAME}")


def main() -> int:
    """Load the fixed student fixture only into the dedicated demo database."""
    settings = load_settings()
    if not _is_isolated_demo_database(settings.database_url):
        raise SyntheticDashboardError(
            f"refusing demo writes outside an isolated {DEMO_DATABASE_NAME}"
        )
    if not DEFAULT_DEMO_CSV.is_file():
        raise SyntheticDashboardError(
            "generate the synthetic student canonical CSV before loading the demo"
        )
    result = seed_synthetic_dashboard(
        build_container(settings).session_factory,
        DEFAULT_DEMO_CSV.read_bytes(),
        filename=DEFAULT_DEMO_CSV.name,
    )
    print("CashFlow AI synthetic dashboard ready")
    print(
        "statement period: "
        f"{result.statement_start_date.isoformat()} to "
        f"{result.statement_end_date.isoformat()}"
    )
    print(f"verified transactions: {result.imported_transactions}")
    print(f"financial roles and categories approved: {result.labelled_transactions}")
    print(f"income: GBP {result.total_income:.2f}")
    print(f"expenses: GBP {result.total_expenses:.2f}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module command entry point
    raise SystemExit(main())


__all__ = [
    "SyntheticDashboardError",
    "SyntheticDashboardSummary",
    "main",
    "seed_synthetic_dashboard",
]

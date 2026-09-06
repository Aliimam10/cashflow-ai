"""Run the review-gated digital-PDF pipeline with fictional local data only."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pymupdf
from sqlalchemy import func, select

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.imports import (
    approve_statement_review,
    extract_text_pdf,
    persist_approved_pdf,
    prepare_statement_review,
    reconstruct_spatial_pdf,
)
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
    FinancialRoleRecord,
    RawTransactionRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.analytics import AnalyticsScope, AnalyticsView
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    DateFormat,
    StatementApproval,
)
from cashflow_ai.schemas.statements import CoverageStatus, DateRange


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import one fictional digital statement into a temporary database."
    )
    parser.add_argument(
        "--order",
        choices=("oldest-first", "newest-first"),
        default="oldest-first",
        help="source row order to exercise (default: oldest-first)",
    )
    return parser.parse_args()


def _statement_pdf(*, newest_first: bool) -> bytes:
    rows = [
        ("05/08/2026", "SYNTHETIC GROCER", "-12.50", "87.50"),
        ("06/08/2026", "SYNTHETIC INCOME", "+40.00", "127.50"),
    ]
    if newest_first:
        rows.reverse()
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    page.insert_text((35, 40), "Fictional digital statement", fontsize=10)
    page.insert_text(
        (35, 60),
        "Statement period: 01 August 2026 to 31 August 2026",
        fontsize=9,
    )
    page.insert_text((35, 80), "Opening balance: GBP 100.00", fontsize=9)
    positions = (35.0, 145.0, 355.0, 465.0)
    for value, x in zip(
        ("Date", "Description", "Amount", "Balance"), positions, strict=True
    ):
        page.insert_text((x, 110), value, fontsize=9)
    for row_index, row in enumerate(rows):
        for value, x in zip(row, positions, strict=True):
            page.insert_text((x, 140 + row_index * 25), value, fontsize=9)
    page.insert_text((35, 210), "Closing balance: GBP 127.50", fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _seed_database(database_path: Path) -> Any:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="demo-profile",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id="demo-account",
                user_profile_id="demo-profile",
                name="Fictional Current Account",
                account_type="current",
                currency="GBP",
                is_active=True,
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))
    return factory


def main() -> None:
    """Execute the synthetic pipeline and print only aggregate verification facts."""
    arguments = _arguments()
    content = _statement_pdf(newest_first=arguments.order == "newest-first")
    spatial = reconstruct_spatial_pdf(content)
    preview = extract_text_pdf(
        content,
        "fictional-digital-statement.pdf",
        mime_type="application/pdf",
        account_id="demo-account",
    )
    review = prepare_statement_review(preview)
    if review.statement_coverage is None or review.balances is None:
        raise RuntimeError("synthetic statement metadata was not reconstructed")
    coverage = review.statement_coverage.model_copy(
        update={"status": CoverageStatus.COMPLETE}
    )
    approved = approve_statement_review(
        review,
        StatementApproval(
            file_hash=review.file_hash,
            approved_at=datetime.now(UTC),
            statement_approved=True,
            date_format=DateFormat.DAY_FIRST,
            sign_convention=AmountSignConvention.SIGNED_AMOUNT,
            confirmed_statement_coverage=coverage,
            confirmed_balances=review.balances,
        ),
    )

    with TemporaryDirectory(prefix="cashflow-pdf-demo-") as temporary_directory:
        factory = _seed_database(Path(temporary_directory) / "demo.db")
        summary = persist_approved_pdf(
            factory,
            content,
            "fictional-digital-statement.pdf",
            mime_type="application/pdf",
            statement=approved,
        )
        with session_scope(factory) as session:
            raw_count = session.scalar(
                select(func.count()).select_from(RawTransactionRecord)
            )
            verified_count = session.scalar(
                select(func.count()).select_from(VerifiedTransactionRecord)
            )
        analytics = compute_cash_flow_analytics(
            factory,
            AnalyticsScope(
                user_profile_id="demo-profile",
                account_ids=("demo-account",),
                period=DateRange(
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 31),
                ),
                view=AnalyticsView.ACCOUNT,
            ),
        )

    if analytics.totals is None:
        raise RuntimeError("synthetic analytics were unexpectedly unavailable")
    print("digital_pdf_demo=passed")
    print(f"source_order={arguments.order}")
    print(f"spatial_state={spatial.state.value}")
    print(f"review_rows={len(review.rows)}")
    print(f"reconciliation={approved.reconciliation.status.value}")
    print(f"persisted_raw_rows={raw_count}")
    print(f"verified_transactions={verified_count}")
    print(f"analytics_transactions={analytics.totals.transaction_count}")
    print(f"financial_roles_pending={analytics.totals.unknown_transaction_count}")
    print(f"imported_transactions={summary.imported_transactions}")
    print("temporary_database_removed=true")


if __name__ == "__main__":
    main()

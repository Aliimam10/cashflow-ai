"""Synthetic digital-PDF extraction, approval, persistence, and analytics proof."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest
from sqlalchemy import func, select

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.imports import (
    approve_statement_review,
    calculate_source_fingerprint,
    extract_text_pdf,
    persist_approved_pdf,
    prepare_statement_review,
)
from cashflow_ai.imports.pdf_persistence import (
    PdfPersistenceError,
    PdfPersistenceErrorCode,
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
    FinancialDataRevisionRecord,
    FinancialRoleRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.analytics import AnalyticsScope, AnalyticsView
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    DateFormat,
    RowDecision,
    RowReview,
    StatementApproval,
)
from cashflow_ai.schemas.statements import CoverageStatus, DateRange


def _fictional_digital_statement(*, newest_first: bool = False) -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    rows = [
        "05/08/2026 | SYNTHETIC SHOP | -10.00 | 90.00",
        "06/08/2026 | SYNTHETIC PAY | +20.00 | 110.00",
        "07/08/2026 | SYNTHETIC ZERO ROW | 0.00 | 110.00",
    ]
    if newest_first:
        rows.reverse()
    lines = (
        "Fictional current account statement",
        "Statement period: 01 August 2026 to 31 August 2026",
        "Opening balance: GBP 100.00",
        "Date | Description | Amount | Balance",
        *rows,
        "Closing balance: GBP 110.00",
    )
    for index, line in enumerate(lines):
        page.insert_text((35, 45 + index * 24), line, fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


@pytest.mark.parametrize("newest_first", [False, True], ids=["oldest", "newest"])
def test_digital_pdf_pipeline_releases_only_reconciled_confirmed_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newest_first: bool,
) -> None:
    database_path = tmp_path / "synthetic-pdf.db"
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="profile-1",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id="account-1",
                user_profile_id="profile-1",
                name="Fictional Current Account",
                account_type="current",
                currency="GBP",
                is_active=True,
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))

    content = _fictional_digital_statement(newest_first=newest_first)
    preview = extract_text_pdf(
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )
    review = prepare_statement_review(preview)
    assert review.statement_coverage is not None
    assert review.balances is not None
    complete_coverage = review.statement_coverage.model_copy(
        update={"status": CoverageStatus.COMPLETE}
    )
    shop_row = next(
        row for row in review.rows if row.original.description_text == "SYNTHETIC SHOP"
    )
    pay_row = next(
        row for row in review.rows if row.original.description_text == "SYNTHETIC PAY"
    )
    zero_row = next(
        row
        for row in review.rows
        if row.original.description_text == "SYNTHETIC ZERO ROW"
    )
    corrected_shop = shop_row.working_draft.model_copy(
        update={
            "description": "SYNTHETIC SHOP CORRECTED",
            "merchant": "Synthetic Shop Corrected",
        }
    )
    approval_time = datetime(2026, 9, 6, 11, 59, tzinfo=UTC)
    approved = approve_statement_review(
        review,
        StatementApproval(
            file_hash=review.file_hash,
            approved_at=approval_time,
            statement_approved=True,
            date_format=DateFormat.DAY_FIRST,
            sign_convention=AmountSignConvention.SIGNED_AMOUNT,
            confirmed_statement_coverage=complete_coverage,
            confirmed_balances=review.balances,
            row_reviews=(
                RowReview(
                    source_fingerprint=shop_row.source_fingerprint,
                    decision=RowDecision.CONFIRM,
                    corrected_draft=corrected_shop,
                ),
                RowReview(
                    source_fingerprint=pay_row.source_fingerprint,
                    decision=RowDecision.CONFIRM,
                ),
                RowReview(
                    source_fingerprint=zero_row.source_fingerprint,
                    decision=RowDecision.REJECT,
                ),
            ),
        ),
    )
    received_at = approval_time + timedelta(minutes=1)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: received_at
    )

    summary = persist_approved_pdf(
        factory,
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        statement=approved,
    )

    assert summary.rows_read == 3
    assert summary.imported_transactions == 2
    assert summary.probable_duplicates == 0
    assert summary.rejected_rows == 1
    with session_scope(factory) as session:
        raw_rows = tuple(
            session.scalars(
                select(RawTransactionRecord).order_by(
                    RawTransactionRecord.page_number,
                    RawTransactionRecord.page_record_number,
                )
            )
        )
        verified_rows = tuple(
            session.scalars(
                select(VerifiedTransactionRecord).order_by(
                    VerifiedTransactionRecord.transaction_date
                )
            )
        )
        assert len(raw_rows) == 3
        assert len(verified_rows) == 2
        shop_raw = next(
            row for row in raw_rows if row.original_description == "SYNTHETIC SHOP"
        )
        assert shop_raw.raw_payload["original"]["signed_amount_text"] == "-10.00"
        assert shop_raw.raw_payload["reviewed_draft"]["description"] == (
            "SYNTHETIC SHOP CORRECTED"
        )
        assert (
            shop_raw.raw_payload["statement_approval"]["reconciliation"]["status"]
            == "reconciled"
        )
        assert [row.amount for row in verified_rows] == [
            Decimal("-10.00"),
            Decimal("20.00"),
        ]
        assert (
            session.scalar(select(func.count()).select_from(StatementCoverageRecord))
            == 1
        )
        revision = session.get(FinancialDataRevisionRecord, "account-1")
        assert revision is not None
        assert revision.revision == 1

    analytics = compute_cash_flow_analytics(
        factory,
        AnalyticsScope(
            user_profile_id="profile-1",
            account_ids=("account-1",),
            period=DateRange(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            ),
            view=AnalyticsView.ACCOUNT,
        ),
    )
    assert analytics.coverage.status.value == "complete"
    assert analytics.totals is not None
    assert analytics.totals.transaction_count == 2
    assert analytics.totals.unknown_transaction_count == 2


def test_fabricated_approved_evidence_cannot_cross_the_pdf_trust_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fabricated-pdf.db"
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="profile-1",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id="account-1",
                user_profile_id="profile-1",
                name="Fictional Current Account",
                account_type="current",
                currency="GBP",
                is_active=True,
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))

    content = _fictional_digital_statement()
    preview = extract_text_pdf(
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )
    review = prepare_statement_review(preview)
    assert review.statement_coverage is not None
    assert review.balances is not None
    zero_row = next(
        row
        for row in review.rows
        if row.original.description_text == "SYNTHETIC ZERO ROW"
    )
    approval_time = datetime(2026, 9, 6, 11, 59, tzinfo=UTC)
    approved = approve_statement_review(
        review,
        StatementApproval(
            file_hash=review.file_hash,
            approved_at=approval_time,
            statement_approved=True,
            date_format=DateFormat.DAY_FIRST,
            sign_convention=AmountSignConvention.SIGNED_AMOUNT,
            confirmed_statement_coverage=review.statement_coverage.model_copy(
                update={"status": CoverageStatus.COMPLETE}
            ),
            confirmed_balances=review.balances,
            row_reviews=(
                RowReview(
                    source_fingerprint=zero_row.source_fingerprint,
                    decision=RowDecision.REJECT,
                ),
            ),
        ),
    )
    genuine_row = approved.rows[0]
    fabricated_original = genuine_row.original.model_copy(
        update={"description_text": "FABRICATED SOURCE DESCRIPTION"}
    )
    fabricated_row = genuine_row.model_copy(
        update={
            "original": fabricated_original,
            "source_fingerprint": calculate_source_fingerprint(
                genuine_row.source_identity,
                fabricated_original,
            ),
        }
    )
    fabricated = approved.model_copy(
        update={"rows": (fabricated_row, *approved.rows[1:])}
    )
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now",
        lambda: approval_time + timedelta(minutes=1),
    )

    with pytest.raises(PdfPersistenceError) as captured:
        persist_approved_pdf(
            factory,
            content,
            "fictional-statement.pdf",
            mime_type="application/pdf",
            statement=fabricated,
        )

    assert captured.value.code is PdfPersistenceErrorCode.INVALID_LINEAGE
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 0
        )

"""Adversarial tests for the exact-byte digital-PDF persistence boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports import (
    PdfPersistenceError,
    PdfPersistenceErrorCode,
    SpatialColumnMapping,
    approve_statement_review,
    calculate_source_fingerprint,
    extract_text_pdf,
    persist_approved_pdf,
    prepare_statement_review,
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
    ImportBatchRecord,
    RawTransactionRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.imports import ImportIssue, IssueSeverity
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    ApprovedStatement,
    DateFormat,
    RowDecision,
    RowReview,
    StatementApproval,
    StatementReview,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    StatementBalances,
)

APPROVED_AT = datetime(2026, 9, 6, 11, 59, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _statement_pdf() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    lines = (
        "Fictional digital statement",
        "Statement period: 01 August 2026 to 31 August 2026",
        "Opening balance: GBP 100.00",
        "Date | Description | Amount | Balance",
        "05/08/2026 | SYNTHETIC SHOP | -10.00 | 90.00",
        "06/08/2026 | SYNTHETIC PAY | +20.00 | 110.00",
        "Closing balance: GBP 110.00",
    )
    for index, line in enumerate(lines):
        page.insert_text((35, 45 + index * 24), line, fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _headerless_spatial_pdf() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    page.insert_text((35, 35), "Fictional spatial statement", fontsize=10)
    page.insert_text(
        (35, 55),
        "Statement period: 01 August 2026 to 31 August 2026",
        fontsize=9,
    )
    page.insert_text((35, 72), "Opening balance: GBP 100.00", fontsize=9)
    positions = (35.0, 145.0, 355.0, 475.0)
    rows = (
        ("05/08/2026", "SYNTHETIC SHOP", "-10.00", "90.00"),
        ("06/08/2026", "SYNTHETIC PAY", "+20.00", "110.00"),
    )
    for row_index, row in enumerate(rows):
        for value, x_position in zip(row, positions, strict=True):
            page.insert_text((x_position, 110 + row_index * 25), value, fontsize=9)
    page.insert_text((35, 175), "Closing balance: GBP 110.00", fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _overlapping_statement_pdf() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    lines = (
        "Fictional overlapping statement",
        "Statement period: 15 August 2026 to 15 September 2026",
        "Opening balance: GBP 110.00",
        "Date | Description | Amount | Balance",
        "20/08/2026 | SYNTHETIC TRAVEL | -5.00 | 105.00",
        "Closing balance: GBP 105.00",
    )
    for index, line in enumerate(lines):
        page.insert_text((35, 45 + index * 24), line, fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'synthetic-trust-boundary.db'}"
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_account(factory: sessionmaker[Session]) -> None:
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


def _review(
    content: bytes,
    *,
    spatial_mapping: SpatialColumnMapping | None = None,
) -> StatementReview:
    preview = extract_text_pdf(
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        account_id="account-1",
        spatial_mapping=spatial_mapping,
    )
    return prepare_statement_review(preview)


def _approve(
    content: bytes,
    *,
    row_reviews: tuple[RowReview, ...] = (),
    balances: StatementBalances | None = None,
    spatial_mapping: SpatialColumnMapping | None = None,
) -> ApprovedStatement:
    review = _review(content, spatial_mapping=spatial_mapping)
    assert review.statement_coverage is not None
    assert review.balances is not None
    coverage = review.statement_coverage.model_copy(
        update={"status": CoverageStatus.COMPLETE}
    )
    return approve_statement_review(
        review,
        StatementApproval(
            file_hash=review.file_hash,
            approved_at=APPROVED_AT,
            statement_approved=True,
            date_format=DateFormat.DAY_FIRST,
            sign_convention=AmountSignConvention.SIGNED_AMOUNT,
            confirmed_statement_coverage=coverage,
            confirmed_balances=review.balances if balances is None else balances,
            row_reviews=row_reviews,
        ),
    )


def test_fabricated_self_consistent_source_evidence_is_rejected(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _statement_pdf()
    approved = _approve(content)
    row = approved.rows[0]
    fabricated_original = row.original.model_copy(
        update={"description_text": "FABRICATED SOURCE ROW"}
    )
    fabricated_fingerprint = calculate_source_fingerprint(
        row.source_identity,
        fabricated_original,
    )
    fabricated_draft = row.extracted_draft.model_copy(
        update={
            "description": "FABRICATED SOURCE ROW",
            "merchant": "Fabricated Source Row",
        }
    )
    fabricated_row = row.model_copy(
        update={
            "source_fingerprint": fabricated_fingerprint,
            "original": fabricated_original,
            "extracted_draft": fabricated_draft,
            "row_decision": RowDecision.CONFIRM,
            "transaction": row.transaction.model_copy(
                update={
                    "description": "FABRICATED SOURCE ROW",
                    "merchant": "Fabricated Source Row",
                }
            ),
        }
    )
    fabricated_statement = approved.model_copy(
        update={"rows": (fabricated_row, approved.rows[1])}
    )
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    with pytest.raises(PdfPersistenceError) as error:
        persist_approved_pdf(
            factory,
            content,
            "fictional-statement.pdf",
            mime_type="application/pdf",
            statement=fabricated_statement,
        )

    assert error.value.code is PdfPersistenceErrorCode.INVALID_LINEAGE
    assert "decisions could not be replayed" in str(error.value)
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 0


def test_edited_extraction_metadata_cannot_cross_the_trusted_boundary(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _statement_pdf()
    approved = _approve(content)
    fabricated_issue = ImportIssue(
        code="fabricated_evidence",
        message="fictional structural evidence",
        severity=IssueSeverity.WARNING,
    )
    tampered = approved.model_copy(
        update={"document_issues": (*approved.document_issues, fabricated_issue)}
    )
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    with pytest.raises(PdfPersistenceError) as error:
        persist_approved_pdf(
            factory,
            content,
            "fictional-statement.pdf",
            mime_type="application/pdf",
            statement=tampered,
        )

    assert error.value.code is PdfPersistenceErrorCode.INVALID_LINEAGE
    assert "fresh extraction" in str(error.value)


def test_explicit_correction_is_replayed_on_fresh_extraction_and_persisted(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _statement_pdf()
    review = _review(content)
    corrected = review.rows[0].working_draft.model_copy(
        update={
            "description": "USER CONFIRMED SYNTHETIC SHOP",
            "merchant": "User Confirmed Synthetic Shop",
        }
    )
    approved = _approve(
        content,
        row_reviews=(
            RowReview(
                source_fingerprint=review.rows[0].source_fingerprint,
                decision=RowDecision.CONFIRM,
                corrected_draft=corrected,
            ),
        ),
    )
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    summary = persist_approved_pdf(
        factory,
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        statement=approved,
    )

    assert summary.imported_transactions == 2
    with session_scope(factory) as session:
        stored = session.scalar(
            select(VerifiedTransactionRecord).where(
                VerifiedTransactionRecord.transaction_date
                == approved.rows[0].transaction.transaction_date
            )
        )
        assert stored is not None
        assert stored.description == "USER CONFIRMED SYNTHETIC SHOP"


def test_explicit_rejection_and_balance_correction_are_replayed_atomically(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _statement_pdf()
    review = _review(content)
    approved = _approve(
        content,
        row_reviews=(
            RowReview(
                source_fingerprint=review.rows[1].source_fingerprint,
                decision=RowDecision.REJECT,
            ),
        ),
        balances=StatementBalances(
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("90.00"),
        ),
    )
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    summary = persist_approved_pdf(
        factory,
        content,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        statement=approved,
    )

    assert summary.imported_transactions == 1
    assert summary.rejected_rows == 1
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 1
        )


def test_explicit_spatial_mapping_is_replayed_against_exact_bytes(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyLayoutPage:
        def extract_tables(self) -> list[object]:
            return []

        def extract_text(self, *, layout: bool) -> str:
            assert layout is True
            return "layout intentionally unavailable"

    class EmptyLayoutPdf:
        def __init__(self) -> None:
            self.pages = [EmptyLayoutPage()]

        def __enter__(self) -> EmptyLayoutPdf:
            return self

        def __exit__(self, *exc_info: object) -> None:
            del exc_info

    monkeypatch.setattr(
        "cashflow_ai.imports.text_pdf.pdfplumber.open",
        lambda stream: EmptyLayoutPdf(),
    )
    content = _headerless_spatial_pdf()
    mapping = SpatialColumnMapping(
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )
    approved = _approve(content, spatial_mapping=mapping)
    assert approved.rows[0].extracted_draft.balance_after is None
    assert "90.00" in {field.value for field in approved.rows[0].original.raw_fields}
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    with pytest.raises(PdfPersistenceError) as missing_mapping:
        persist_approved_pdf(
            factory,
            content,
            "fictional-spatial.pdf",
            mime_type="application/pdf",
            statement=approved,
        )
    assert missing_mapping.value.code is PdfPersistenceErrorCode.INVALID_LINEAGE
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 0

    summary = persist_approved_pdf(
        factory,
        content,
        "fictional-spatial.pdf",
        mime_type="application/pdf",
        statement=approved,
        spatial_mapping=mapping,
    )

    assert summary.imported_transactions == 2
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 2
        )


def test_overlapping_pdf_statements_report_existing_coverage_atomically(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_content = _statement_pdf()
    second_content = _overlapping_statement_pdf()
    first = _approve(first_content)
    second = _approve(second_content)
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    persist_approved_pdf(
        factory,
        first_content,
        "first-statement.pdf",
        mime_type="application/pdf",
        statement=first,
    )
    summary = persist_approved_pdf(
        factory,
        second_content,
        "overlapping-statement.pdf",
        mime_type="application/pdf",
        statement=second,
    )

    assert summary.coverage.previous_statement_count == 1
    assert summary.coverage.overlap_periods == (
        DateRange(start_date=date(2026, 8, 15), end_date=date(2026, 8, 31)),
    )
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 2
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 3
        )

"""Tests for stateless digital-PDF HTTP review and mapping contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas.pdf_api import (
    DigitalPdfColumn,
    DigitalPdfColumnMapping,
    DigitalPdfColumnRole,
    DigitalPdfMappingPreview,
    DigitalPdfMappingRow,
    DigitalPdfReviewResult,
    DigitalPdfReviewState,
)
from cashflow_ai.schemas.reconciliation import StatementReview

HASH_A = "a" * 64
HASH_B = "b" * 64


def _review(*, file_hash: str = HASH_A) -> StatementReview:
    return StatementReview.model_validate(
        {
            "file_hash": file_hash,
            "source_type": "digital_pdf",
            "statement_coverage": None,
            "balances": None,
            "balance_evidence": [],
            "document_issues": [],
            "rows": [
                {
                    "source_identity": {
                        "source_type": "digital_pdf",
                        "source_document_hash": file_hash,
                        "page_number": 1,
                        "page_record_number": 1,
                    },
                    "source_fingerprint": HASH_B,
                    "original": {
                        "transaction_date_text": "01/08/2026",
                        "description_text": "SYNTHETIC SHOP",
                        "signed_amount_text": "-10.00",
                        "raw_fields": [{"column": "Date", "value": "01/08/2026"}],
                    },
                    "extracted_draft": {
                        "transaction_date": "2026-08-01",
                        "description": "SYNTHETIC SHOP",
                        "amount": "-10.00",
                        "currency": "GBP",
                        "account_id": "synthetic-account",
                        "direction": "outflow",
                    },
                    "working_draft": {
                        "transaction_date": "2026-08-01",
                        "description": "SYNTHETIC SHOP",
                        "amount": "-10.00",
                        "currency": "GBP",
                        "account_id": "synthetic-account",
                        "direction": "outflow",
                    },
                    "provenance": {
                        "source_type": "digital_pdf",
                        "method": "pdf_text",
                        "page_number": 1,
                    },
                }
            ],
            "reconciliation": {
                "status": "unavailable",
                "opening_balance": None,
                "signed_transaction_total": "-10.00",
                "expected_closing_balance": None,
                "closing_balance": None,
                "unexplained_difference": None,
                "unusable_transaction_count": 0,
            },
            "ocr_confidence_threshold": 0.85,
            "requires_date_format_confirmation": True,
            "requires_debit_credit_sign_confirmation": False,
        }
    )


def _mapping_preview(
    *,
    sample_values: tuple[str, ...] = ("01/08/2026", "SYNTHETIC SHOP", "-10.00"),
    total_rows: int = 1,
    truncated: bool = False,
) -> DigitalPdfMappingPreview:
    return DigitalPdfMappingPreview(
        file_hash=HASH_A,
        structure_digest=HASH_B,
        page_count=1,
        columns=(
            DigitalPdfColumn(
                column_id="column_1",
                header_text="Date",
                role_hint=DigitalPdfColumnRole.TRANSACTION_DATE,
            ),
            DigitalPdfColumn(
                column_id="column_2",
                header_text="Description",
                role_hint=DigitalPdfColumnRole.DESCRIPTION,
            ),
            DigitalPdfColumn(
                column_id="column_3",
                header_text="Amount",
                role_hint=DigitalPdfColumnRole.SIGNED_AMOUNT,
            ),
        ),
        sample_rows=(
            DigitalPdfMappingRow(
                page_number=1,
                page_record_number=1,
                values=sample_values,
            ),
        ),
        total_rows=total_rows,
        truncated=truncated,
    )


def test_mapping_accepts_signed_or_complete_debit_credit_layouts() -> None:
    signed = DigitalPdfColumnMapping(
        file_hash=HASH_A,
        structure_digest=HASH_B,
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )
    separate = DigitalPdfColumnMapping(
        file_hash=HASH_A,
        structure_digest=HASH_B,
        transaction_date="column_1",
        description="column_2",
        debit_amount="column_3",
        credit_amount="column_4",
    )

    assert signed.signed_amount == "column_3"
    assert separate.credit_amount == "column_4"


@pytest.mark.parametrize(
    "values",
    [
        {"debit_amount": "column_3"},
        {"credit_amount": "column_3"},
        {
            "signed_amount": "column_3",
            "debit_amount": "column_4",
            "credit_amount": "column_5",
        },
        {},
        {"signed_amount": "column_1"},
    ],
)
def test_mapping_rejects_incomplete_ambiguous_or_duplicate_roles(
    values: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        DigitalPdfColumnMapping(
            file_hash=HASH_A,
            structure_digest=HASH_B,
            transaction_date="column_1",
            description="column_2",
            **values,
        )


def test_mapping_preview_requires_aligned_bounded_rows() -> None:
    preview = _mapping_preview()
    assert preview.total_rows == 1
    with pytest.raises(ValidationError, match="align"):
        _mapping_preview(sample_values=("01/08/2026", "SYNTHETIC SHOP"))
    with pytest.raises(ValidationError, match="cannot exceed"):
        DigitalPdfMappingPreview(
            file_hash=preview.file_hash,
            structure_digest=preview.structure_digest,
            page_count=preview.page_count,
            columns=preview.columns,
            sample_rows=(
                preview.sample_rows[0],
                preview.sample_rows[0].model_copy(update={"page_record_number": 2}),
            ),
            total_rows=1,
            truncated=False,
        )
    with pytest.raises(ValidationError, match="truncation"):
        _mapping_preview(total_rows=2, truncated=False)


def test_review_result_enforces_payload_for_each_state() -> None:
    ready = DigitalPdfReviewResult(
        state=DigitalPdfReviewState.READY,
        file_hash=HASH_A,
        reason_code="review_ready",
        guidance="Review the values.",
        review=_review(),
    )
    mapping = DigitalPdfReviewResult(
        state=DigitalPdfReviewState.MAPPING_REQUIRED,
        file_hash=HASH_A,
        reason_code="column_mapping_required",
        guidance="Map the columns.",
        mapping_preview=_mapping_preview(),
    )
    unsupported = DigitalPdfReviewResult(
        state=DigitalPdfReviewState.UNSUPPORTED_LAYOUT,
        file_hash=HASH_A,
        reason_code="unknown_layout",
        guidance="Use CSV.",
        recommended_format="csv",
    )

    assert ready.review is not None
    assert mapping.mapping_preview is not None
    assert unsupported.recommended_format == "csv"


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "ready"},
        {"state": "ready", "review": _review(file_hash=HASH_B)},
        {"state": "ready", "review": _review(), "recommended_format": "csv"},
        {"state": "mapping_required"},
        {
            "state": "mapping_required",
            "mapping_preview": _mapping_preview().model_copy(
                update={"file_hash": HASH_B}
            ),
        },
        {
            "state": "mapping_required",
            "mapping_preview": _mapping_preview(),
            "recommended_format": "csv",
        },
        {"state": "unsupported_layout"},
        {
            "state": "unsupported_layout",
            "review": _review(),
            "recommended_format": "csv",
        },
    ],
)
def test_review_result_rejects_state_payload_mismatches(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DigitalPdfReviewResult.model_validate(
            {
                "file_hash": HASH_A,
                "reason_code": "synthetic_reason",
                "guidance": "Synthetic guidance.",
                **payload,
            }
        )

"""Validation tests for statement-workspace public contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas.csv_imports import CsvColumnMapping
from cashflow_ai.schemas.pdf_api import DigitalPdfColumnMapping
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCsvDownload,
    WorkspaceDuplicateDecision,
    WorkspaceEditRequest,
    WorkspaceFileMapping,
    WorkspaceFinalizeResult,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
    WorkspaceSourceFile,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
HASH = "a" * 64


def _row(**changes: object) -> WorkspaceTransactionRow:
    values: dict[str, object] = {
        "row_id": "row-1",
        "transaction_date": date(2026, 8, 1),
        "description": "FICTIONAL SHOP",
        "amount": Decimal("-10.00"),
        "financial_role": FinancialRole.EXPENSE,
        "review_state": WorkspaceRowReviewState.CONFIRMED,
    }
    values.update(changes)
    return WorkspaceTransactionRow.model_validate(values)


def _coverage(**changes: object) -> WorkspaceCoverageConfirmation:
    values: dict[str, object] = {
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 31),
        "status": CoverageStatus.COMPLETE,
        "confirmed": True,
    }
    values.update(changes)
    return WorkspaceCoverageConfirmation.model_validate(values)


def _workspace(**changes: object) -> StatementWorkspace:
    values: dict[str, object] = {
        "workspace_id": "workspace-1",
        "retention_mode": WorkspaceRetentionMode.SAVED,
        "status": WorkspaceStatus.DRAFT,
        "account_name": "Fictional current",
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return StatementWorkspace.model_validate(values)


def test_file_mapping_is_exact_file_and_source_type_bound() -> None:
    csv_mapping = CsvColumnMapping(
        transaction_date_column="Date",
        description_column="Description",
        signed_amount_column="Amount",
    )
    valid = WorkspaceFileMapping(
        file_hash=HASH,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=csv_mapping,
    )
    assert valid.csv_mapping == csv_mapping

    with pytest.raises(ValidationError, match="CSV sources require only"):
        WorkspaceFileMapping(
            file_hash=HASH,
            source_type=WorkspaceSourceType.CSV,
        )
    with pytest.raises(ValidationError, match="digital PDFs require only"):
        WorkspaceFileMapping(
            file_hash=HASH,
            source_type=WorkspaceSourceType.DIGITAL_PDF,
            csv_mapping=csv_mapping,
        )
    with pytest.raises(ValidationError, match="unsupported files cannot"):
        WorkspaceFileMapping(
            file_hash=HASH,
            source_type=WorkspaceSourceType.UNSUPPORTED,
            csv_mapping=csv_mapping,
        )
    with pytest.raises(ValidationError, match="exact workspace file"):
        WorkspaceFileMapping(
            file_hash=HASH,
            source_type=WorkspaceSourceType.DIGITAL_PDF,
            pdf_mapping=DigitalPdfColumnMapping(
                file_hash="b" * 64,
                structure_digest="c" * 64,
                transaction_date="column_1",
                description="column_2",
                signed_amount="column_3",
            ),
        )


def test_source_review_payload_matches_its_state() -> None:
    mapping = WorkspaceSourceFile(
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        display_name="fictional.csv",
        file_hash=HASH,
        state=WorkspaceSourceReviewState.MAPPING_REQUIRED,
        reason_code="ambiguous",
        guidance="Map columns.",
        row_count=0,
        mapping_columns=("Date", "Amount"),
        mapping_sample_rows=(("2026-08-01", "-2.00"),),
    )
    assert mapping.mapping_columns == ("Date", "Amount")

    base = {
        "source_id": "source-1",
        "source_type": WorkspaceSourceType.CSV,
        "display_name": "fictional.csv",
        "file_hash": HASH,
        "reason_code": "review",
        "guidance": "Review safely.",
    }
    invalid_payloads = (
        {
            **base,
            "state": WorkspaceSourceReviewState.MAPPING_REQUIRED,
            "row_count": 0,
        },
        {
            **base,
            "state": WorkspaceSourceReviewState.MAPPING_REQUIRED,
            "row_count": 0,
            "mapping_columns": ("Date", "Amount"),
            "mapping_sample_rows": (("2026-08-01",),),
        },
        {
            **base,
            "source_type": WorkspaceSourceType.DIGITAL_PDF,
            "state": WorkspaceSourceReviewState.MAPPING_REQUIRED,
            "row_count": 0,
            "mapping_columns": ("Date", "Amount"),
            "mapping_sample_rows": (("2026-08-01", "-2.00"),),
        },
        {
            **base,
            "state": WorkspaceSourceReviewState.READY,
            "row_count": 1,
            "mapping_columns": ("Date",),
            "mapping_sample_rows": (("2026-08-01",),),
        },
        {
            **base,
            "state": WorkspaceSourceReviewState.READY,
            "row_count": 1,
            "mapping_structure_digest": "b" * 64,
        },
        {
            **base,
            "state": WorkspaceSourceReviewState.READY,
            "row_count": 0,
        },
        {
            **base,
            "state": WorkspaceSourceReviewState.UNSUPPORTED,
            "row_count": 1,
        },
        {
            **base,
            "source_type": WorkspaceSourceType.UNSUPPORTED,
            "state": WorkspaceSourceReviewState.READY,
            "row_count": 1,
        },
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            WorkspaceSourceFile.model_validate(payload)


def test_mapping_evidence_is_bounded_and_sensitive_values_are_hidden_from_repr() -> (
    None
):
    source = WorkspaceSourceFile(
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        display_name="PRIVATE-FICTIONAL-NAME.csv",
        file_hash=HASH,
        state=WorkspaceSourceReviewState.MAPPING_REQUIRED,
        reason_code="ambiguous",
        guidance="Map columns.",
        row_count=0,
        mapping_columns=("Date", "Description", "Amount"),
        mapping_sample_rows=(("2026-08-01", "PRIVATE DESCRIPTION", "-12.34"),),
    )
    assert "PRIVATE-FICTIONAL-NAME" not in repr(source)
    assert "PRIVATE DESCRIPTION" not in repr(source)
    assert HASH not in repr(source)

    row = _row(description="PRIVATE DESCRIPTION", amount=Decimal("-12.34"))
    workspace = _workspace(
        account_name="PRIVATE ACCOUNT LABEL",
        sources=(source,),
        rows=(row,),
    )
    assert "PRIVATE DESCRIPTION" not in repr(row)
    assert "12.34" not in repr(row)
    assert "PRIVATE ACCOUNT LABEL" not in repr(workspace)

    download = WorkspaceCsvDownload(
        filename="approved.csv",
        content="description,amount\nPRIVATE DESCRIPTION,-12.34\n",
        row_count=1,
    )
    assert "PRIVATE DESCRIPTION" not in repr(download)

    base = source.model_dump()
    with pytest.raises(ValidationError):
        WorkspaceSourceFile.model_validate(
            {
                **base,
                "mapping_columns": tuple(f"column_{index}" for index in range(21)),
                "mapping_sample_rows": (("value",) * 21,),
            }
        )
    with pytest.raises(ValidationError):
        WorkspaceSourceFile.model_validate(
            {
                **base,
                "mapping_sample_rows": (("x" * 1_001, "value", "value"),),
            }
        )


@pytest.mark.parametrize(
    ("amount", "role"),
    [
        (Decimal("10.00"), FinancialRole.EXPENSE),
        (Decimal("-10.00"), FinancialRole.INCOME),
    ],
)
def test_confirmed_row_requires_complete_sign_compatible_values(
    amount: Decimal,
    role: FinancialRole,
) -> None:
    with pytest.raises(ValidationError, match="financial role"):
        _row(amount=amount, financial_role=role)
    with pytest.raises(ValidationError, match="require date"):
        _row(transaction_date=None)

    with pytest.raises(ValidationError, match="identity and type"):
        _row(source_id="source-1")
    with pytest.raises(ValidationError, match="source coordinates"):
        _row(source_record_number=1)
    with pytest.raises(ValidationError, match="unsupported sources"):
        _row(source_id="source-1", source_type=WorkspaceSourceType.UNSUPPORTED)


def test_duplicate_and_rejected_row_states_remain_coherent() -> None:
    probable = _row(
        review_state=WorkspaceRowReviewState.NEEDS_REVIEW,
        probable_duplicate_of="row-original",
        issue_codes=("probable_duplicate",),
    )
    assert probable.probable_duplicate_of == "row-original"
    with pytest.raises(ValidationError, match="visible issue"):
        _row(
            review_state=WorkspaceRowReviewState.NEEDS_REVIEW,
            probable_duplicate_of="row-original",
            issue_codes=(),
        )
    with pytest.raises(ValidationError, match="rejected rows"):
        _row(
            review_state=WorkspaceRowReviewState.REJECTED,
            probable_duplicate_of="row-original",
        )


def test_row_revision_and_edit_request_require_unambiguous_decisions() -> None:
    base: dict[str, object] = {
        "row_id": "row-1",
        "expected_revision": 1,
        "transaction_date": date(2026, 8, 1),
        "description": "FICTIONAL SHOP",
        "amount": Decimal("-10.00"),
        "financial_role": FinancialRole.EXPENSE,
        "review_state": WorkspaceRowReviewState.CONFIRMED,
    }
    revision = WorkspaceRowRevision.model_validate(base)
    assert revision.amount == Decimal("-10.00")
    with pytest.raises(ValidationError, match="non-zero amount"):
        WorkspaceRowRevision.model_validate({**base, "amount": None})
    with pytest.raises(ValidationError, match="financial role"):
        WorkspaceRowRevision.model_validate({**base, "amount": Decimal("10.00")})
    with pytest.raises(ValidationError, match="kept duplicate"):
        WorkspaceRowRevision.model_validate(
            {
                **base,
                "review_state": WorkspaceRowReviewState.REJECTED,
                "duplicate_decision": WorkspaceDuplicateDecision.KEEP,
            }
        )
    with pytest.raises(ValidationError, match="only once"):
        WorkspaceEditRequest(
            expected_workspace_revision=1,
            rows=(revision, revision),
        )


def test_coverage_rejects_invalid_gaps_and_accepts_ordered_gaps() -> None:
    first = DateRange(start_date=date(2026, 8, 4), end_date=date(2026, 8, 5))
    second = DateRange(start_date=date(2026, 8, 10), end_date=date(2026, 8, 11))
    coverage = _coverage(
        status=CoverageStatus.GAPPED,
        missing_periods=(first, second),
    )
    assert coverage.missing_periods == (first, second)

    invalid = (
        {"start_date": date(2026, 9, 1), "end_date": date(2026, 8, 1)},
        {
            "status": CoverageStatus.GAPPED,
            "missing_periods": (
                DateRange(start_date=date(2026, 7, 31), end_date=date(2026, 8, 2)),
            ),
        },
        {"status": CoverageStatus.GAPPED, "missing_periods": ()},
        {"status": CoverageStatus.COMPLETE, "missing_periods": (first,)},
        {
            "status": CoverageStatus.GAPPED,
            "missing_periods": (
                first,
                DateRange(start_date=date(2026, 8, 5), end_date=date(2026, 8, 7)),
            ),
        },
    )
    for changes in invalid:
        with pytest.raises(ValidationError):
            _coverage(**changes)


def test_workspace_lifecycle_requires_utc_chronology_and_resolved_rows() -> None:
    coverage = _coverage()
    source = WorkspaceSourceFile(
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        display_name="fictional.csv",
        file_hash=HASH,
        state=WorkspaceSourceReviewState.READY,
        reason_code="ready",
        guidance="Review.",
        row_count=1,
    )
    finalized = _workspace(
        status=WorkspaceStatus.FINALIZED,
        rows=(_row(),),
        coverage=coverage,
        finalized_at=NOW,
    )
    assert finalized.unresolved_row_count == 0
    assert (
        _workspace(
            rows=(_row(review_state=WorkspaceRowReviewState.READY),)
        ).unresolved_row_count
        == 1
    )

    invalid = (
        {"status": WorkspaceStatus.FINALIZED, "rows": (_row(),)},
        {
            "status": WorkspaceStatus.FINALIZED,
            "rows": (_row(review_state=WorkspaceRowReviewState.READY),),
            "coverage": coverage,
            "finalized_at": NOW,
        },
        {"status": WorkspaceStatus.DRAFT, "finalized_at": NOW},
        {"coverage": coverage},
        {
            "balance": WorkspaceBalanceConfirmation(
                balance=Decimal("90.00"),
                as_of_date=date(2026, 8, 31),
                confirmed=True,
            )
        },
        {"created_at": NOW, "updated_at": NOW - timedelta(seconds=1)},
        {
            "created_at": NOW.astimezone(timezone(timedelta(hours=1))),
            "updated_at": NOW.astimezone(timezone(timedelta(hours=1))),
        },
        {
            "status": WorkspaceStatus.FINALIZED,
            "rows": (_row(),),
            "coverage": coverage,
            "updated_at": NOW + timedelta(seconds=1),
            "finalized_at": NOW,
        },
        {
            "status": WorkspaceStatus.FINALIZED,
            "rows": (_row(review_state=WorkspaceRowReviewState.REJECTED),),
            "coverage": coverage,
            "finalized_at": NOW,
        },
        {
            "status": WorkspaceStatus.FINALIZED,
            "sources": (source,),
            "rows": (_row(),),
            "coverage": coverage,
            "finalized_at": NOW,
        },
        {
            "status": WorkspaceStatus.FINALIZED,
            "rows": (
                _row(
                    source_id="source-1",
                    source_type=WorkspaceSourceType.CSV,
                ),
            ),
            "coverage": coverage,
            "finalized_at": NOW,
        },
    )
    for changes in invalid:
        with pytest.raises(ValidationError):
            _workspace(**changes)


def test_finalization_result_matches_the_finalized_workspace() -> None:
    finalized = _workspace(
        status=WorkspaceStatus.FINALIZED,
        rows=(_row(),),
        coverage=_coverage(),
        finalized_at=NOW,
    )
    result = WorkspaceFinalizeResult(
        workspace=finalized,
        included_rows=1,
        rejected_rows=2,
        persisted=True,
    )
    assert result.included_rows == 1

    with pytest.raises(ValidationError, match="does not match"):
        WorkspaceFinalizeResult(
            workspace=finalized,
            included_rows=2,
            rejected_rows=0,
            persisted=True,
        )
    with pytest.raises(ValidationError, match="does not match"):
        WorkspaceFinalizeResult(
            workspace=finalized,
            included_rows=1,
            rejected_rows=0,
            persisted=False,
        )
    with pytest.raises(ValidationError, match="does not match"):
        WorkspaceFinalizeResult(
            workspace=_workspace(rows=(_row(),)),
            included_rows=1,
            rejected_rows=0,
            persisted=True,
        )

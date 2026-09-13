"""Typed loopback-client tests for statement workspace operations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx2
import pytest

from cashflow_ai.frontend.client import (
    ApiClient,
    ApiClientError,
    ApiClientErrorCode,
    UploadedDocument,
)
from cashflow_ai.schemas.csv_imports import CsvColumnMapping
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCreateRequest,
    WorkspaceDeleteAllRequest,
    WorkspaceDeleteRequest,
    WorkspaceEditRequest,
    WorkspaceFileMapping,
    WorkspaceFinalizeRequest,
    WorkspaceFinalizeResult,
    WorkspaceImportReview,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
    WorkspaceSourceFile,
    WorkspaceSourceRemoveRequest,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
FILE_HASH = "a" * 64


def _workspace(*, finalized: bool = False) -> StatementWorkspace:
    row = WorkspaceTransactionRow(
        row_id="row-1",
        transaction_date=date(2026, 8, 1),
        description="SYNTHETIC SHOP",
        amount=Decimal("-10.00"),
        category_id="other",
        financial_role=FinancialRole.EXPENSE,
        review_state=(
            WorkspaceRowReviewState.CONFIRMED
            if finalized
            else WorkspaceRowReviewState.READY
        ),
    )
    coverage = WorkspaceCoverageConfirmation(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        status=CoverageStatus.COMPLETE,
        confirmed=True,
    )
    return StatementWorkspace(
        workspace_id="workspace-1",
        retention_mode=WorkspaceRetentionMode.SAVED,
        status=WorkspaceStatus.FINALIZED if finalized else WorkspaceStatus.DRAFT,
        account_name="Fictional account",
        revision=2 if finalized else 1,
        sources=(
            ()
            if finalized
            else (
                WorkspaceSourceFile(
                    source_id="source-1",
                    source_type=WorkspaceSourceType.CSV,
                    display_name="fictional.csv",
                    file_hash=FILE_HASH,
                    state=WorkspaceSourceReviewState.READY,
                    reason_code="review_ready",
                    guidance="Review rows.",
                    row_count=1,
                ),
            )
        ),
        rows=(row,),
        coverage=coverage if finalized else None,
        created_at=NOW,
        updated_at=NOW,
        finalized_at=NOW if finalized else None,
    )


def test_workspace_client_sends_typed_json_and_multi_file_requests() -> None:
    requests: list[httpx2.Request] = []
    draft = _workspace()
    final = _workspace(finalized=True)
    review = WorkspaceImportReview(
        workspace=draft,
        accepted_files=1,
        mapping_required_files=0,
        unsupported_files=0,
        exact_duplicates_removed=0,
        probable_duplicates=0,
    )
    finalize_result = WorkspaceFinalizeResult(
        workspace=final,
        included_rows=1,
        rejected_rows=0,
        persisted=True,
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/review"):
            return httpx2.Response(200, json=review.model_dump(mode="json"))
        if path.endswith("/rows"):
            return httpx2.Response(200, json=draft.model_dump(mode="json"))
        if path.endswith("/finalize"):
            return httpx2.Response(200, json=finalize_result.model_dump(mode="json"))
        if path.endswith("/download"):
            return httpx2.Response(
                200,
                json={
                    "filename": "cashflow-approved-transactions.csv",
                    "content": "date,description\n2026-08-01,SYNTHETIC SHOP\n",
                    "row_count": 1,
                },
            )
        if "/sources/" in path:
            return httpx2.Response(200, json=draft.model_dump(mode="json"))
        if request.method == "DELETE" and path == "/api/v1/workspaces":
            return httpx2.Response(
                200,
                json={
                    "active_workspaces_deleted": 1,
                    "saved_workspaces_deleted": 1,
                    "deleted": True,
                },
            )
        if request.method == "DELETE":
            return httpx2.Response(
                200,
                json={"workspace_id": "workspace-1", "deleted": True},
            )
        return httpx2.Response(
            201 if request.method == "POST" else 200,
            json=draft.model_dump(mode="json"),
        )

    document = UploadedDocument(
        filename="fictional.csv",
        content=b"Date,Description,Amount\n2026-08-01,SYNTHETIC SHOP,-10.00\n",
        mime_type="text/csv",
    )
    mapping = WorkspaceFileMapping(
        file_hash=FILE_HASH,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Amount",
        ),
    )
    edit = WorkspaceEditRequest(
        expected_workspace_revision=1,
        rows=(
            WorkspaceRowRevision(
                row_id="row-1",
                expected_revision=1,
                transaction_date=date(2026, 8, 1),
                description="SYNTHETIC SHOP",
                amount=Decimal("-10.00"),
                financial_role=FinancialRole.EXPENSE,
                review_state=WorkspaceRowReviewState.CONFIRMED,
            ),
        ),
    )
    finalize = WorkspaceFinalizeRequest(
        expected_workspace_revision=1,
        statement_confirmed=True,
        date_interpretation_confirmed=True,
        sign_convention_confirmed=True,
        coverage=WorkspaceCoverageConfirmation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("90.00"),
            as_of_date=date(2026, 8, 31),
            confirmed=True,
        ),
    )

    with ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(handler),
    ) as client:
        assert client.create_workspace(WorkspaceCreateRequest()) == draft
        assert client.get_workspace("workspace-1") == draft
        assert client.latest_saved_workspace() == draft
        assert client.review_workspace_files("workspace-1", (document,)) == review
        assert (
            client.review_workspace_files(
                "workspace-1",
                (document, document),
                mappings=(mapping,),
            )
            == review
        )
        assert client.edit_workspace("workspace-1", edit) == draft
        assert (
            client.remove_workspace_source(
                "workspace-1",
                "source-1",
                WorkspaceSourceRemoveRequest(expected_workspace_revision=1),
            )
            == draft
        )
        assert client.finalize_workspace("workspace-1", finalize) == finalize_result
        assert client.download_workspace_csv("workspace-1").row_count == 1
        assert client.delete_workspace(
            "workspace-1", WorkspaceDeleteRequest(confirmed=True)
        ).deleted
        erased = client.delete_all_workspace_data(
            WorkspaceDeleteAllRequest(confirmed=True)
        )
        assert erased.active_workspaces_deleted == 1
        assert erased.saved_workspaces_deleted == 1

    reviews = tuple(item for item in requests if item.url.path.endswith("/review"))
    assert b"mappings_json" not in reviews[0].content
    assert reviews[1].content.count(b'filename="fictional.csv"') == 2
    assert b"mappings_json" in reviews[1].content
    assert b"transaction_date_column" in reviews[1].content
    assert (
        next(item for item in requests if item.url.path.endswith("/rows")).method
        == "PATCH"
    )
    assert any(item.url.path.endswith("/sources/source-1") for item in requests)
    assert any(
        item.method == "DELETE" and item.url.path == "/api/v1/workspaces"
        for item in requests
    )


def test_latest_saved_workspace_returns_none_only_for_its_controlled_absence() -> None:
    def missing(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            404,
            json={
                "code": "workspace_not_found",
                "message": "no saved statement workspace is available",
            },
        )

    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(missing),
    )
    assert client.latest_saved_workspace() is None
    client.close()

    def other_error(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            503,
            json={"code": "database_unavailable", "message": "try later"},
        )

    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(other_error),
    )
    with pytest.raises(ApiClientError) as captured:
        client.latest_saved_workspace()
    assert captured.value.problem_code == "database_unavailable"
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        {"code": "http_error", "message": "the HTTP request cannot be served"},
        {"detail": "Not Found", "private": "SYNTHETIC PRIVATE RESPONSE"},
    ],
)
def test_workspace_client_explains_an_outdated_api_without_echoing_body(
    body: object,
) -> None:
    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(404, json=body, request=request)
        ),
    )

    with pytest.raises(ApiClientError) as captured:
        client.create_workspace(WorkspaceCreateRequest())

    assert captured.value.code is ApiClientErrorCode.API_VERSION_MISMATCH
    assert captured.value.status_code == 404
    assert "run make api again" in str(captured.value)
    assert "SYNTHETIC PRIVATE RESPONSE" not in str(captured.value)
    client.close()


def test_client_rejects_mixed_single_and_multiple_upload_fields() -> None:
    document = UploadedDocument(
        filename="fictional.csv",
        content=b"fictional",
        mime_type="text/csv",
    )
    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
    )
    with pytest.raises(ApiClientError) as captured:
        client._request(
            "POST",
            "/private-test",
            StatementWorkspace,
            document=document,
            documents=(document,),
        )
    assert captured.value.code is ApiClientErrorCode.INVALID_CONFIGURATION
    client.close()

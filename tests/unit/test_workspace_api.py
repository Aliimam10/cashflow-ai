"""HTTP tests for the session-based statement-workspace boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from cashflow_ai.api import AppContainer, build_container, create_app
from cashflow_ai.api import routes as api_routes
from cashflow_ai.api import uploads as api_uploads
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.persistence import Base

CSV = (
    b"Date,Description,Amount,Balance,Transaction ID\n"
    b"2026-08-01,SYNTHETIC RENT,-400.00,600.00,rent-1\n"
    b"2026-08-15,SYNTHETIC PAY,1000.00,1600.00,pay-1\n"
)
AMBIGUOUS = b"When,What,Money\n01/08/2026,SYNTHETIC SHOP,-10.00\n"


def _container(tmp_path: Path) -> AppContainer:
    settings = Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        database_url=f"sqlite:///{tmp_path / 'workspace-api.db'}",
        api_host="127.0.0.1",
        api_port=8765,
    )
    container = build_container(settings)
    Base.metadata.create_all(container.engine)
    return container


def _create(client: TestClient, *, retention: str = "saved") -> dict[str, Any]:
    response = client.post(
        "/api/v1/workspaces",
        json={
            "retention_mode": retention,
            "account_name": "Fictional current account",
            "currency": "GBP",
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _review(client: TestClient, workspace_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/review",
        files=[
            ("files", ("first.csv", CSV, "text/csv")),
            (
                "files",
                (
                    "second.csv",
                    b"Date,Description,Amount\n2026-08-20,SYNTHETIC FOOD,-20.00\n",
                    "text/csv",
                ),
            ),
        ],
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def _confirm_rows(client: TestClient, workspace: dict[str, Any]) -> dict[str, Any]:
    revisions = []
    for row in workspace["rows"]:
        positive = float(row["amount"]) > 0
        revisions.append(
            {
                "row_id": row["row_id"],
                "expected_revision": row["revision"],
                "transaction_date": row["transaction_date"],
                "description": row["description"],
                "amount": row["amount"],
                "balance_after": row["balance_after"],
                "category_id": "income" if positive else "other",
                "financial_role": "income" if positive else "expense",
                "review_state": "confirmed",
            }
        )
    response = client.patch(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rows",
        json={
            "expected_workspace_revision": workspace["revision"],
            "rows": revisions,
        },
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def test_workspace_http_flow_is_blank_review_gated_and_data_minimised(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        missing = client.get("/api/v1/workspaces/latest-saved")
        assert missing.status_code == 404
        assert missing.json()["code"] == "workspace_not_found"
        assert missing.headers["cache-control"] == "no-store, max-age=0"
        assert missing.headers["pragma"] == "no-cache"

        created = _create(client)
        workspace_id = cast(str, created["workspace_id"])
        assert created["rows"] == []
        assert client.get(f"/api/v1/workspaces/{workspace_id}").json() == created

        review = _review(client, workspace_id)
        assert review["accepted_files"] == 2
        assert len(review["workspace"]["rows"]) == 3
        edited = _confirm_rows(client, review["workspace"])

        blocked = client.post(
            f"/api/v1/workspaces/{workspace_id}/finalize",
            json={
                "expected_workspace_revision": edited["revision"],
                "statement_confirmed": True,
                "date_interpretation_confirmed": True,
                "sign_convention_confirmed": True,
                "coverage": {
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-31",
                    "status": "complete",
                    "missing_periods": [],
                    "confirmed": True,
                },
            },
        )
        assert blocked.status_code == 400
        assert blocked.json()["code"] == "workspace_review_required"

        finalized = client.post(
            f"/api/v1/workspaces/{workspace_id}/finalize",
            json={
                "expected_workspace_revision": edited["revision"],
                "statement_confirmed": True,
                "date_interpretation_confirmed": True,
                "sign_convention_confirmed": True,
                "coverage": {
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-31",
                    "status": "complete",
                    "missing_periods": [],
                    "confirmed": True,
                },
                "balance": {
                    "balance": "1600.00",
                    "as_of_date": "2026-08-31",
                    "currency": "GBP",
                    "confirmed": True,
                },
            },
        )
        assert finalized.status_code == 200
        assert finalized.headers["cache-control"] == "no-store, max-age=0"
        finalized_workspace = finalized.json()["workspace"]
        assert finalized_workspace["sources"] == []
        assert all(row["source_id"] is None for row in finalized_workspace["rows"])

        latest = client.get("/api/v1/workspaces/latest-saved")
        assert latest.status_code == 200
        assert latest.json() == finalized_workspace
        download = client.get(f"/api/v1/workspaces/{workspace_id}/download")
        assert download.status_code == 200
        assert "SYNTHETIC RENT" in download.json()["content"]

        invalid_delete = client.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}",
            json={"confirmed": False},
        )
        assert invalid_delete.status_code == 422
        deleted = client.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}",
            json={"confirmed": True},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"workspace_id": workspace_id, "deleted": True}

    assert container.workspace_store.get(workspace_id) is None


def test_delete_all_workspace_route_requires_confirmation_and_clears_memory(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        _create(client, retention="temporary")
        _create(client, retention="temporary")
        rejected = client.request(
            "DELETE",
            "/api/v1/workspaces",
            json={"confirmed": False},
        )
        assert rejected.status_code == 422

        deleted = client.request(
            "DELETE",
            "/api/v1/workspaces",
            json={"confirmed": True},
        )

    assert deleted.status_code == 200
    assert deleted.json() == {
        "active_workspaces_deleted": 2,
        "saved_workspaces_deleted": 0,
        "deleted": True,
    }
    assert deleted.headers["cache-control"] == "no-store, max-age=0"
    assert container.workspace_store.count() == 0


def test_workspace_mapping_form_is_exact_file_bound_and_privacy_safe(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        created = _create(client, retention="temporary")
        workspace_id = cast(str, created["workspace_id"])
        review = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={"files": ("ambiguous.csv", AMBIGUOUS, "text/csv")},
        )
        assert review.status_code == 200
        source = review.json()["workspace"]["sources"][0]
        assert source["state"] == "mapping_required"

        invalid = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={"files": ("ambiguous.csv", AMBIGUOUS, "text/csv")},
            data={"mappings_json": "not-json"},
        )
        assert invalid.status_code == 400
        assert invalid.json() == {
            "code": "workspace_invalid_mapping",
            "message": "the workspace file mappings are invalid",
            "validation_issues": [],
            "page_numbers": [],
        }
        assert "SYNTHETIC SHOP" not in invalid.text

        mapping = [
            {
                "file_hash": source["file_hash"],
                "source_type": "csv",
                "csv_mapping": {
                    "transaction_date_column": "When",
                    "description_column": "What",
                    "signed_amount_column": "Money",
                },
            }
        ]
        too_many_mappings = json.dumps(mapping * 21)
        excessive = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={"files": ("ambiguous.csv", AMBIGUOUS, "text/csv")},
            data={"mappings_json": too_many_mappings},
        )
        assert excessive.status_code == 400
        assert (
            excessive.json()["message"] == "no more than 20 file mappings are accepted"
        )

        oversized_mapping = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={"files": ("ambiguous.csv", AMBIGUOUS, "text/csv")},
            data={"mappings_json": "x" * 65_537},
        )
        assert oversized_mapping.status_code == 422
        assert "xxxxx" not in oversized_mapping.text

        mapped = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files={"files": ("ambiguous.csv", AMBIGUOUS, "text/csv")},
            data={"mappings_json": json.dumps(mapping)},
        )
        assert mapped.status_code == 200
        assert mapped.json()["workspace"]["sources"][0]["state"] == "ready"


def test_workspace_routes_reject_untrusted_host_without_state_change(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        rejected = client.post(
            "/api/v1/workspaces",
            headers={"host": "cashflow.attacker.example"},
            json={
                "retention_mode": "temporary",
                "account_name": "PRIVATE INPUT MUST NOT ECHO",
                "currency": "GBP",
            },
        )

    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_host_header"
    assert "PRIVATE INPUT MUST NOT ECHO" not in rejected.text
    assert rejected.headers["cache-control"] == "no-store, max-age=0"
    assert container.workspace_store.count() == 0


def test_workspace_review_rejects_more_than_twenty_files_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_calls = 0

    async def fail_if_read(upload: UploadFile, *, max_bytes: int) -> bytes:
        nonlocal read_calls
        del upload, max_bytes
        read_calls += 1
        return b""

    monkeypatch.setattr(api_routes, "read_bounded_upload", fail_if_read)
    with TestClient(create_app(_container(tmp_path))) as client:
        workspace_id = cast(str, _create(client, retention="temporary")["workspace_id"])
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files=[
                (
                    "files",
                    (f"statement-{index}.csv", b"PRIVATE SOURCE VALUE", "text/csv"),
                )
                for index in range(21)
            ],
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "workspace_invalid_upload",
        "message": "upload between 1 and 20 statement files",
        "validation_issues": [],
        "page_numbers": [],
    }
    assert read_calls == 0
    assert "PRIVATE SOURCE VALUE" not in response.text


def test_workspace_review_stops_at_the_cumulative_upload_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_limits: list[int] = []
    original_read = api_uploads.read_bounded_upload

    async def observed_read(upload: UploadFile, *, max_bytes: int) -> bytes:
        read_limits.append(max_bytes)
        return await original_read(upload, max_bytes=max_bytes)

    monkeypatch.setattr(api_routes, "MAX_WORKSPACE_UPLOAD_BYTES", 10)
    monkeypatch.setattr(api_routes, "read_bounded_upload", observed_read)
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        workspace_id = cast(str, _create(client, retention="temporary")["workspace_id"])
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files=[
                ("files", ("one.csv", b"123456", "text/csv")),
                ("files", ("two.csv", b"PRIVATE SOURCE VALUE", "text/csv")),
            ],
        )
        workspace = container.workspace_store.get(workspace_id)

    assert response.status_code == 413
    assert response.json() == {
        "code": "workspace_upload_too_large",
        "message": "the combined statement upload is too large",
        "validation_issues": [],
        "page_numbers": [],
    }
    assert read_limits == [10, 4]
    assert "PRIVATE SOURCE VALUE" not in response.text
    assert workspace is not None
    assert workspace.sources == ()
    assert workspace.rows == ()


def test_unsupported_source_can_be_removed_without_resetting_valid_uploads(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        created = _create(client, retention="temporary")
        workspace_id = cast(str, created["workspace_id"])
        reviewed = client.post(
            f"/api/v1/workspaces/{workspace_id}/review",
            files=[
                ("files", ("valid.csv", CSV, "text/csv")),
                ("files", ("notes.txt", b"fictional", "text/plain")),
            ],
        )
        assert reviewed.status_code == 200
        workspace = reviewed.json()["workspace"]
        unsupported = next(
            source
            for source in workspace["sources"]
            if source["state"] == "unsupported"
        )

        removed = client.request(
            "DELETE",
            (f"/api/v1/workspaces/{workspace_id}/sources/{unsupported['source_id']}"),
            json={"expected_workspace_revision": workspace["revision"]},
        )

        assert removed.status_code == 200
        assert len(removed.json()["sources"]) == 1
        assert len(removed.json()["rows"]) == 2


def test_workspace_download_and_state_errors_are_controlled(tmp_path: Path) -> None:
    with TestClient(create_app(_container(tmp_path))) as client:
        created = _create(client, retention="temporary")
        workspace_id = cast(str, created["workspace_id"])
        download = client.get(f"/api/v1/workspaces/{workspace_id}/download")
        assert download.status_code == 400
        assert download.json()["code"] == "workspace_not_finalized"

        missing_delete = client.request(
            "DELETE",
            "/api/v1/workspaces/missing",
            json={"confirmed": True},
        )
        assert missing_delete.status_code == 404
        assert missing_delete.json()["code"] == "workspace_not_found"

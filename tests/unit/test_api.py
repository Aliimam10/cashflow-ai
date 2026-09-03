"""Tests for the local FastAPI foundation and ingestion routes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy.exc import OperationalError

from cashflow_ai.api import AppContainer, build_container, create_app
from cashflow_ai.api import cli as api_cli
from cashflow_ai.api import routes as api_routes
from cashflow_ai.api.demo import main as api_demo_main
from cashflow_ai.api.services import check_readiness
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.imports import OcrWord, PdfImportError, PdfImportErrorCode
from cashflow_ai.persistence import Base
from cashflow_ai.persistence.base import new_id
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    StatementCoverageRecord,
)
from cashflow_ai.persistence.repositories import AccountRepository
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
    StatementFlag,
)
from cashflow_ai.schemas.transactions import FinancialRole

_CSV = (
    b"Date,Description,Amount,Balance\n"
    b"2026-08-01,SYNTHETIC SALARY,1000.00,1000.00\n"
    b"2026-08-02,SYNTHETIC RENT,-400.00,600.00\n"
)


class AvailableOcrEngine:
    """Deterministic local OCR replacement containing fictional text only."""

    def ensure_available(self) -> None:
        return None

    def detect_orientation(self, image: Image.Image) -> tuple[int, float] | None:
        assert image.mode == "L"
        return 0, 0.99

    def recognise_words(self, image: Image.Image) -> tuple[OcrWord, ...]:
        assert image.mode == "L"
        lines = (
            "Fictional Example Bank",
            "Statement period: 01 August 2026 to 31 August 2026",
            "Opening balance: GBP 100.00",
            "Date | Description | Amount | Balance",
            "01/08/2026 | SYNTHETIC SHOP | -10.00 | 90.00",
            "Closing balance: GBP 90.00",
        )
        return tuple(
            OcrWord(
                text=line,
                confidence=0.92,
                block_number=1,
                paragraph_number=1,
                line_number=index,
            )
            for index, line in enumerate(lines, start=1)
        )


class UnavailableOcrEngine(AvailableOcrEngine):
    """Deterministic missing-Tesseract replacement."""

    def ensure_available(self) -> None:
        raise PdfImportError(
            PdfImportErrorCode.OCR_ENGINE_UNAVAILABLE,
            "synthetic local OCR unavailable",
        )


@dataclass(frozen=True, slots=True)
class ApiHarness:
    client: TestClient
    container: AppContainer


def _settings(database_url: str) -> Settings:
    return Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        database_url=database_url,
        api_host="127.0.0.1",
        api_port=8765,
    )


def _container(
    tmp_path: Path,
    *,
    name: str = "api.db",
    ocr_factory: Any = AvailableOcrEngine,
    create_schema: bool = True,
) -> AppContainer:
    base = build_container(_settings(f"sqlite:///{tmp_path / name}"))
    container = AppContainer(
        settings=base.settings,
        engine=base.engine,
        session_factory=base.session_factory,
        ocr_engine_factory=ocr_factory,
    )
    if create_schema:
        Base.metadata.create_all(container.engine)
        with session_scope(container.session_factory) as session:
            session.add_all(
                FinancialRoleRecord(id=role.value, name=role.value.replace("_", " "))
                for role in FinancialRole
            )
    return container


@pytest.fixture
def api(tmp_path: Path) -> Iterator[ApiHarness]:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        yield ApiHarness(client=client, container=container)


def _profile_and_account(client: TestClient) -> tuple[str, str]:
    profile = client.post(
        "/api/v1/profiles",
        json={
            "display_name": "Fictional User",
            "base_currency": "GBP",
            "timezone": "Europe/London",
        },
    )
    assert profile.status_code == 201
    profile_id = cast(str, profile.json()["profile_id"])
    account = client.post(
        f"/api/v1/profiles/{profile_id}/accounts",
        json={
            "name": "Fictional Current Account",
            "account_type": "current",
            "currency": "GBP",
            "institution_label": "Example Bank",
        },
    )
    assert account.status_code == 201
    return profile_id, cast(str, account.json()["account_id"])


def _csv_plan(account_id: str, *, include_balances: bool = True) -> CsvImportPlan:
    return CsvImportPlan(
        account_id=account_id,
        statement_context=ImportContext(
            account_id=account_id,
            coverage=StatementCoverage(
                statement_start_date=date(2026, 8, 1),
                statement_end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
            ),
            balances=(
                StatementBalances(
                    opening_balance=Decimal("0.00"),
                    closing_balance=Decimal("600.00"),
                )
                if include_balances
                else None
            ),
            flags=frozenset({StatementFlag.CONTAINS_UNUSUAL_ONE_OFF_EXPENSES}),
            note="Fictional context only",
        ),
        mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Amount",
            running_balance_column="Balance",
        ),
    )


def _confirm_csv(client: TestClient, account_id: str) -> dict[str, Any]:
    preview = client.post(
        "/api/v1/imports/csv/preview",
        files={"file": ("synthetic.csv", _CSV, "text/csv")},
    )
    assert preview.status_code == 200
    confirmation = CsvImportConfirmation(
        preview_file_hash=preview.json()["file_hash"],
        user_confirmed=True,
        confirmed_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    response = client.post(
        "/api/v1/imports/csv/confirm",
        files={"file": ("synthetic.csv", _CSV, "text/csv")},
        data={
            "plan_json": _csv_plan(account_id).model_dump_json(),
            "confirmation_json": confirmation.model_dump_json(),
        },
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def _finish_pdf(document: Any) -> bytes:
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _text_pdf() -> bytes:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    lines = (
        "Fictional Example Bank",
        "Statement period: 01 August 2026 to 31 August 2026",
        "Opening balance: GBP 100.00",
        "Date | Description | Amount | Balance",
        "2026-08-01 | SYNTHETIC SHOP | -10.00 | 90.00",
        "Closing balance: GBP 90.00",
    )
    for index, line in enumerate(lines):
        page.insert_text((45, 45 + index * 22), line, fontsize=10)
    return _finish_pdf(document)


def _scanned_pdf() -> bytes:
    image = Image.new("RGB", (240, 320), "white")
    drawing = ImageDraw.Draw(image)
    drawing.text((15, 20), "Fictional scanned statement", fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=240, height=320)
    page.insert_image(  # type: ignore[no-untyped-call]
        page.rect, stream=output.getvalue()
    )
    return _finish_pdf(document)


def test_health_readiness_and_openapi_include_decision_support(api: ApiHarness) -> None:
    health = api.client.get("/health")
    ready = api.client.get("/ready")
    schema = api.client.get("/openapi.json").json()

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": "0.1.0"}
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "database_connection": True,
        "database_schema": True,
    }
    assert api.client.get("/docs").status_code == 200
    paths = set(schema["paths"])
    assert "/api/v1/imports/csv/preview" in paths
    assert "/api/v1/imports/pdf/ocr/preview" in paths
    assert "/api/v1/accounts/{account_id}/transactions" in paths
    assert "/api/v1/analytics/cash-flow" in paths
    assert "/api/v1/forecasts/balance" in paths
    assert "/api/v1/planning/evaluate" in paths
    assert "/api/v1/anomalies/detect" in paths


def test_synthetic_api_demo_exercises_role_analytics_and_pagination(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_demo_main()

    output = capsys.readouterr().out
    assert "verified transactions imported: 2" in output
    assert "role-aware cash flow: income=1000.00 expenses=400.00 net=600.00" in output
    assert "coverage status: complete" in output
    assert "analytics freshness: current" in output
    assert "pagination: returned=1 total=2" in output
    assert "raw source payload returned: false" in output
    assert "temporary database retained: false" in output


def test_readiness_reports_missing_schema_and_connection_failure(
    tmp_path: Path,
) -> None:
    missing_schema = _container(tmp_path, name="empty.db", create_schema=False)
    with TestClient(create_app(missing_schema)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "database_connection": True,
            "database_schema": False,
        }

    broken = _container(tmp_path, name="broken.db", create_schema=False)

    def fail_connect() -> Any:
        raise OperationalError("SELECT 1", {}, RuntimeError("synthetic failure"))

    broken.engine.connect = fail_connect  # type: ignore[method-assign]
    assert check_readiness(broken.engine).model_dump() == {
        "status": "not_ready",
        "database_connection": False,
        "database_schema": False,
    }
    broken.engine.dispose()


def test_profile_and_account_routes_enforce_single_local_owner(api: ApiHarness) -> None:
    assert api.client.get("/api/v1/profiles/current").status_code == 404
    profile_id, account_id = _profile_and_account(api.client)

    current = api.client.get("/api/v1/profiles/current")
    by_id = api.client.get(f"/api/v1/profiles/{profile_id}")
    account = api.client.get(f"/api/v1/accounts/{account_id}")
    accounts = api.client.get(f"/api/v1/profiles/{profile_id}/accounts")

    assert current.status_code == by_id.status_code == account.status_code == 200
    assert current.json()["profile_id"] == profile_id
    assert by_id.json()["timezone"] == "Europe/London"
    assert account.json()["institution_label"] == "Example Bank"
    assert accounts.json()["total"] == 1
    assert [item["account_id"] for item in accounts.json()["items"]] == [account_id]
    assert (
        api.client.post("/api/v1/profiles", json={"timezone": "UTC"}).status_code == 409
    )
    duplicate = api.client.post(
        f"/api/v1/profiles/{profile_id}/accounts",
        json={"name": "fictional current account", "account_type": "savings"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "account_name_exists"
    assert api.client.get("/api/v1/profiles/missing").status_code == 404
    assert api.client.get("/api/v1/accounts/missing").status_code == 404
    assert api.client.get("/api/v1/profiles/missing/accounts").status_code == 404
    assert (
        api.client.post(
            "/api/v1/profiles/missing/accounts",
            json={"name": "Fictional Savings", "account_type": "savings"},
        ).status_code
        == 404
    )


def test_request_validation_never_echoes_private_input(api: ApiHarness) -> None:
    private_value = "PRIVATE DESCRIPTION " * 40
    response = api.client.post(
        "/api/v1/profiles",
        json={"display_name": private_value, "timezone": "Mars/Olympus"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
    assert response.json()["validation_issues"]
    assert private_value not in response.text


def test_csv_preview_confirmation_context_and_transaction_reads(
    api: ApiHarness,
) -> None:
    _, account_id = _profile_and_account(api.client)
    preview = api.client.post(
        "/api/v1/imports/csv/preview",
        files={"file": ("../synthetic.csv", _CSV, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["source_filename"] == "synthetic.csv"
    assert preview.json()["total_data_rows"] == 2

    imported = _confirm_csv(api.client, account_id)
    assert imported["new_transactions"] == 2
    batch_id = cast(str, imported["import_batch_id"])
    context = api.client.get(f"/api/v1/imports/{batch_id}/context")
    transactions = api.client.get(f"/api/v1/accounts/{account_id}/transactions")
    assert transactions.json()["total"] == 2
    transaction_id = transactions.json()["items"][0]["transaction_id"]
    transaction = api.client.get(f"/api/v1/transactions/{transaction_id}")

    assert context.status_code == 200
    assert context.json()["context"]["balances"] == {
        "currency": "GBP",
        "opening_balance": "0.00",
        "closing_balance": "600.00",
    }
    assert context.json()["context"]["note"] == "Fictional context only"
    assert transactions.status_code == transaction.status_code == 200
    assert len(transactions.json()["items"]) == 2
    assert transaction.json()["description"] == "SYNTHETIC SALARY"
    assert "raw_payload" not in transaction.json()
    assert "original_description" not in transaction.json()


def test_csv_errors_have_stable_http_statuses_and_no_body_echo(api: ApiHarness) -> None:
    _, account_id = _profile_and_account(api.client)
    unsupported = api.client.post(
        "/api/v1/imports/csv/preview",
        files={"file": ("statement.txt", _CSV, "text/plain")},
    )
    malformed = api.client.post(
        "/api/v1/imports/csv/preview",
        files={"file": ("statement.csv", b"private malformed", "text/csv")},
    )
    oversized = api.client.post(
        "/api/v1/imports/csv/preview",
        files={
            "file": (
                "statement.csv",
                b"x" * (10 * 1024 * 1024 + 1),
                "text/csv",
            )
        },
    )
    invalid_form = api.client.post(
        "/api/v1/imports/csv/confirm",
        files={"file": ("statement.csv", _CSV, "text/csv")},
        data={"plan_json": "private malformed", "confirmation_json": "{}"},
    )
    changed = CsvImportConfirmation(
        preview_file_hash="0" * 64,
        user_confirmed=True,
        confirmed_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    conflict = api.client.post(
        "/api/v1/imports/csv/confirm",
        files={"file": ("statement.csv", _CSV, "text/csv")},
        data={
            "plan_json": _csv_plan(account_id).model_dump_json(),
            "confirmation_json": changed.model_dump_json(),
        },
    )

    assert unsupported.status_code == 415
    assert malformed.status_code == 400
    assert oversized.status_code == 413
    assert invalid_form.status_code == 422
    assert "private malformed" not in invalid_form.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "preview_changed"


def test_text_pdf_preview_review_and_confirmation_are_non_persistent(
    api: ApiHarness,
) -> None:
    _, account_id = _profile_and_account(api.client)
    content = _text_pdf()
    preview = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={"account_id": account_id, "account_currency": "GBP"},
    )
    assert preview.status_code == 200
    assert preview.json()["candidates"][0]["draft"]["amount"] == "-10.00"

    review = api.client.post(
        "/api/v1/imports/pdf/review",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={
            "source_type": "digital_pdf",
            "account_id": account_id,
            "account_currency": "GBP",
        },
    )
    assert review.status_code == 200
    approval = {
        "file_hash": review.json()["file_hash"],
        "approved_at": "2026-09-02T00:00:00Z",
        "statement_approved": True,
        "confirmed_statement_coverage": review.json()["statement_coverage"],
        "confirmed_balances": review.json()["balances"],
        "row_reviews": [],
    }
    confirmed = api.client.post(
        "/api/v1/imports/pdf/confirm",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={
            "source_type": "digital_pdf",
            "account_id": account_id,
            "account_currency": "GBP",
            "approval_json": json.dumps(approval),
        },
    )
    mismatch = api.client.post(
        "/api/v1/imports/pdf/confirm",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={
            "source_type": "digital_pdf",
            "account_id": account_id,
            "approval_json": json.dumps({**approval, "file_hash": "0" * 64}),
        },
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["rows"][0]["transaction"]["description"] == (
        "SYNTHETIC SHOP"
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "file_changed"
    assert api.client.get(f"/api/v1/accounts/{account_id}/transactions").json() == {
        "items": [],
        "limit": 50,
        "offset": 0,
        "total": 0,
    }


def test_pdf_upload_errors_route_to_ocr_and_enforce_limits(api: ApiHarness) -> None:
    _, account_id = _profile_and_account(api.client)
    scanned = _scanned_pdf()
    unsupported_name = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("statement.txt", scanned, "application/pdf")},
        data={"account_id": account_id},
    )
    unsupported_mime = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("statement.pdf", scanned, "text/plain")},
        data={"account_id": account_id},
    )
    requires_ocr = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("statement.pdf", scanned, "application/pdf")},
        data={"account_id": account_id},
    )
    oversized = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={
            "file": (
                "statement.pdf",
                b"%PDF-" + b"x" * (20 * 1024 * 1024),
                "application/pdf",
            )
        },
        data={"account_id": account_id},
    )
    malformed = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("statement.pdf", b"%PDF-private", "application/pdf")},
        data={"account_id": account_id},
    )

    assert unsupported_name.status_code == unsupported_mime.status_code == 415
    assert requires_ocr.status_code == 409
    assert requires_ocr.json()["code"] == "ocr_required"
    assert requires_ocr.json()["page_numbers"] == [1]
    assert oversized.status_code == 413
    assert malformed.status_code == 400
    assert "private" not in malformed.text


def test_ocr_status_and_preview_use_replaceable_local_engine(api: ApiHarness) -> None:
    _, account_id = _profile_and_account(api.client)
    status_response = api.client.get("/api/v1/ocr/status")
    preview = api.client.post(
        "/api/v1/imports/pdf/ocr/preview",
        files={"file": ("synthetic-scan.pdf", _scanned_pdf(), "application/pdf")},
        data={"account_id": account_id, "account_currency": "GBP"},
    )
    review = api.client.post(
        "/api/v1/imports/pdf/review",
        files={"file": ("synthetic-scan.pdf", _scanned_pdf(), "application/pdf")},
        data={"source_type": "ocr_pdf", "account_id": account_id},
    )

    assert status_response.status_code == 200
    assert status_response.json()["available"] is True
    assert status_response.json()["execution"] == "local_only"
    assert preview.status_code == 200
    assert preview.json()["pages"][0]["confidence"] == pytest.approx(0.92)
    assert preview.json()["candidates"][0]["line_numbers"] == [5]
    assert review.status_code == 200
    assert review.json()["source_type"] == "ocr_pdf"


def test_unavailable_ocr_is_reported_without_bypassing_review(tmp_path: Path) -> None:
    container = _container(tmp_path, ocr_factory=UnavailableOcrEngine)
    with TestClient(create_app(container)) as client:
        _, account_id = _profile_and_account(client)
        status_response = client.get("/api/v1/ocr/status")
        preview = client.post(
            "/api/v1/imports/pdf/ocr/preview",
            files={"file": ("synthetic-scan.pdf", _scanned_pdf(), "application/pdf")},
            data={"account_id": account_id},
        )

    assert status_response.status_code == 200
    assert status_response.json()["available"] is False
    assert preview.status_code == 409
    assert preview.json()["code"] == "ocr_engine_unavailable"


def test_pdf_account_guards_reject_missing_inactive_or_wrong_currency(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id, account_id = _profile_and_account(api.client)
    content = _text_pdf()
    missing = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={"account_id": "missing"},
    )
    invalid_currency = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={"account_id": account_id, "account_currency": "USD"},
    )
    assert missing.status_code == 404
    assert invalid_currency.status_code == 422

    original_get = AccountRepository.get

    def mismatched_account(
        repository: AccountRepository, requested_account_id: str
    ) -> AccountRecord | None:
        record = original_get(repository, requested_account_id)
        if record is not None:
            record.currency = "USD"
        return record

    monkeypatch.setattr(AccountRepository, "get", mismatched_account)
    mismatch = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={"account_id": account_id},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "account_currency_mismatch"
    monkeypatch.setattr(AccountRepository, "get", original_get)

    with session_scope(api.container.session_factory) as session:
        record = session.get(AccountRecord, account_id)
        assert record is not None
        record.is_active = False
    inactive = api.client.post(
        "/api/v1/imports/pdf/text/preview",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
        data={"account_id": account_id},
    )
    assert inactive.status_code == 409
    assert inactive.json()["code"] == "account_inactive"

    second = api.client.post(
        f"/api/v1/profiles/{profile_id}/accounts",
        json={"name": "Fictional Savings", "account_type": "savings"},
    )
    assert second.status_code == 201


def test_missing_resources_and_context_without_balances_are_explicit(
    api: ApiHarness,
) -> None:
    _, account_id = _profile_and_account(api.client)
    assert api.client.get("/api/v1/accounts/missing/transactions").status_code == 404
    assert api.client.get("/api/v1/transactions/missing").status_code == 404
    assert api.client.get("/api/v1/imports/missing/context").status_code == 404

    incomplete_id = new_id()
    context_batch_id = new_id()
    context_id = new_id()
    with session_scope(api.container.session_factory) as session:
        session.add_all(
            (
                ImportBatchRecord(
                    id=incomplete_id,
                    account_id=account_id,
                    source_type="csv",
                    source_filename="synthetic-incomplete.csv",
                    file_hash="1" * 64,
                    mime_type="text/csv",
                    byte_size=10,
                    verification_status="unverified",
                    imported_at=datetime(2026, 9, 2, tzinfo=UTC),
                ),
                ImportBatchRecord(
                    id=context_batch_id,
                    account_id=account_id,
                    source_type="csv",
                    source_filename="synthetic-context.csv",
                    file_hash="2" * 64,
                    mime_type="text/csv",
                    byte_size=10,
                    verification_status="verified",
                    imported_at=datetime(2026, 9, 2, tzinfo=UTC),
                ),
            )
        )
        session.flush()
        session.add(
            ImportContextRecord(
                id=context_id,
                import_batch_id=context_batch_id,
                flags_json=[StatementFlag.CONTAINS_REFUNDS.value],
                note=None,
                created_at=datetime(2026, 9, 2, tzinfo=UTC),
            )
        )
        session.flush()
        session.add(
            StatementCoverageRecord(
                id=new_id(),
                import_context_id=context_id,
                statement_start_date=date(2026, 8, 1),
                statement_end_date=date(2026, 8, 31),
                coverage_status="complete",
                missing_periods_json=[],
            )
        )

    incomplete = api.client.get(f"/api/v1/imports/{incomplete_id}/context")
    context = api.client.get(f"/api/v1/imports/{context_batch_id}/context")
    assert incomplete.status_code == 409
    assert incomplete.json()["code"] == "import_context_unavailable"
    assert context.status_code == 200
    assert context.json()["context"]["balances"] is None
    assert context.json()["context"]["flags"] == ["contains_refunds"]


def test_http_database_and_unexpected_errors_are_sanitised(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_found = api.client.get("/unknown-private-route")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "http_error"

    def database_failure(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OperationalError("private SQL", {}, RuntimeError("private failure"))

    monkeypatch.setattr(api_routes, "get_account", database_failure)
    database_client = TestClient(api.client.app, raise_server_exceptions=False)
    database = database_client.get("/api/v1/accounts/anything")
    assert database.status_code == 503
    assert database.json()["code"] == "database_unavailable"
    assert "private" not in database.text

    def unexpected_failure(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("private transaction description")

    monkeypatch.setattr(api_routes, "get_account", unexpected_failure)
    unexpected = database_client.get("/api/v1/accounts/anything")
    assert unexpected.status_code == 500
    assert unexpected.json()["code"] == "internal_error"
    assert "private transaction description" not in unexpected.text


def test_default_factory_cli_and_manual_demo_are_runnable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    default_app = create_app()
    assert default_app.debug is False
    with TestClient(default_app) as client:
        assert client.get("/health").status_code == 200

    container = _container(tmp_path, name="cli.db")
    calls: dict[str, Any] = {}
    sentinel_app = create_app(container)
    monkeypatch.setattr(api_cli, "load_settings", lambda: container.settings)
    monkeypatch.setattr(api_cli, "configure_logging", lambda settings: None)
    monkeypatch.setattr(api_cli, "build_container", lambda settings: container)
    monkeypatch.setattr(api_cli, "create_app", lambda supplied: sentinel_app)

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr("cashflow_ai.api.cli.uvicorn.run", fake_run)
    api_cli.main()
    assert calls == {
        "app": sentinel_app,
        "host": "127.0.0.1",
        "port": 8765,
        "log_config": None,
    }
    container.engine.dispose()

    from cashflow_ai.api.demo import main as demo_main

    demo_main()
    output = capsys.readouterr().out
    assert "health: ok" in output
    assert "CSV preview rows: 2" in output
    assert "verified transactions imported: 2" in output
    assert "raw source payload returned: false" in output
    assert "temporary database retained: false" in output

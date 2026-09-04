"""Cross-boundary tests for complete workflows and privacy safeguards."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from cashflow_ai.api import AppContainer, build_container, create_app
from cashflow_ai.categorisation import categorise_verified_transactions
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.imports import OcrWord
from cashflow_ai.persistence import Base, session_scope
from cashflow_ai.persistence.models import (
    CategoryRecord,
    FinancialRoleRecord,
    RawTransactionRecord,
)
from cashflow_ai.schemas import (
    CategorisationPlan,
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
    DateRange,
    ForecastDatasetPlan,
    ForecastModelPolicy,
    ForecastPathPlan,
    ForecastPathPolicy,
    FreshnessPolicy,
    ImportContext,
    StatementBalances,
    StatementCoverage,
    load_category_rule_set,
    load_taxonomy,
)
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole


class DecimalErrorOcrEngine:
    """Return deterministic fictional OCR with one deliberate decimal error."""

    def __init__(self) -> None:
        self.processed_images: list[Image.Image] = []

    def ensure_available(self) -> None:
        return None

    def detect_orientation(
        self, image: Image.Image
    ) -> tuple[Literal[0, 90, 180, 270], float] | None:
        return 0, 0.99

    def recognise_words(self, image: Image.Image) -> tuple[OcrWord, ...]:
        self.processed_images.append(image)
        lines = (
            ("Fictional Example Bank", 0.99),
            ("Statement period: 01 August 2026 to 31 August 2026", 0.99),
            ("Opening balance: GBP 100.00", 0.99),
            ("Date | Description | Amount | Balance", 0.99),
            ("04/08/2026 | SYNTHETIC CAMERA SHOP | -450 | 95.50", 0.51),
            ("Closing balance: GBP 95.50", 0.99),
        )
        return tuple(
            OcrWord(
                text=text,
                confidence=confidence,
                block_number=1,
                paragraph_number=1,
                line_number=index,
            )
            for index, (text, confidence) in enumerate(lines, start=1)
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
        api_port=8765,
    )


def _container(
    tmp_path: Path,
    *,
    ocr_engine: DecimalErrorOcrEngine | None = None,
) -> AppContainer:
    base = build_container(_settings(f"sqlite:///{tmp_path / 'integration.db'}"))
    engine = ocr_engine or DecimalErrorOcrEngine()
    container = AppContainer(
        settings=base.settings,
        engine=base.engine,
        session_factory=base.session_factory,
        ocr_engine_factory=lambda: engine,
    )
    Base.metadata.create_all(container.engine)
    taxonomy = load_taxonomy(Path("configs/categories.yaml"))
    with session_scope(container.session_factory) as session:
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            CategoryRecord(
                id=item.id,
                name=item.name,
                parent_id=item.parent_id,
                taxonomy_version=taxonomy.version,
                is_active=item.is_active,
            )
            for item in taxonomy.categories
        )
    return container


@pytest.fixture
def integration_api(tmp_path: Path) -> Iterator[tuple[TestClient, AppContainer]]:
    container = _container(tmp_path)
    with TestClient(create_app(container)) as client:
        yield client, container


def _create_profile_and_account(client: TestClient) -> tuple[str, str]:
    profile = client.post(
        "/api/v1/profiles",
        json={
            "display_name": "Fictional Workflow User",
            "base_currency": "GBP",
            "timezone": "Europe/London",
        },
    )
    profile.raise_for_status()
    profile_id = cast(str, profile.json()["profile_id"])
    account = client.post(
        f"/api/v1/profiles/{profile_id}/accounts",
        json={
            "name": "Fictional Workflow Current Account",
            "account_type": "current",
            "currency": "GBP",
            "institution_label": "Example Bank",
        },
    )
    account.raise_for_status()
    return profile_id, cast(str, account.json()["account_id"])


def _synthetic_year_csv(as_of_date: date) -> tuple[bytes, date, Decimal]:
    start = as_of_date - timedelta(days=370)
    opening_balance = Decimal("1000.00")
    running_balance = opening_balance
    rows: list[tuple[date, str, Decimal, Decimal]] = []
    first_monday = start + timedelta(days=(-start.weekday()) % 7)
    week_start = first_monday
    week_index = 0
    while week_start <= as_of_date:
        running_balance += Decimal("300.00")
        rows.append(
            (week_start, "SYNTHETIC SALARY", Decimal("300.00"), running_balance)
        )
        expense_date = min(week_start + timedelta(days=1), as_of_date)
        expense = Decimal(20 + (week_index % 6) * 9)
        running_balance -= expense
        rows.append(
            (
                expense_date,
                "SYNTHETIC SUPERMARKET",
                -expense,
                running_balance,
            )
        )
        week_start += timedelta(weeks=1)
        week_index += 1
    rows.sort(key=lambda row: (row[0], -row[2]))
    content = ["Date,Description,Amount,Balance"]
    content.extend(
        f"{row_date.isoformat()},{description},{amount:.2f},{balance:.2f}"
        for row_date, description, amount, balance in rows
    )
    return ("\n".join(content) + "\n").encode(), start, running_balance


def _forecast_payload(
    *,
    profile_id: str,
    account_id: str,
    start: date,
    as_of_date: date,
    cutoff: datetime,
) -> dict[str, Any]:
    forecast_start = cutoff.date() + timedelta(days=(7 - cutoff.date().weekday()) % 7)
    if forecast_start == cutoff.date():
        forecast_start += timedelta(days=7)
    dataset_plan = ForecastDatasetPlan(
        user_profile_id=profile_id,
        account_ids=(account_id,),
        period=DateRange(start_date=start, end_date=as_of_date),
        knowledge_cutoff_at=cutoff,
        payday_days=(1, 15),
    )
    model_policy = ForecastModelPolicy(
        initial_training_weeks=8,
        final_test_weeks=4,
        minimum_training_weeks=8,
        minimum_relative_mae_improvement=0.05,
        maximum_relative_rmse_regression=0,
        maximum_absolute_bias_increase=Decimal("1.00"),
        maximum_iterations=30,
        learning_rate=0.1,
        maximum_leaf_nodes=10,
        minimum_samples_leaf=2,
        random_seed=37,
    )
    path_plan = ForecastPathPlan(
        user_profile_id=profile_id,
        account_id=account_id,
        forecast_start=forecast_start,
        horizon_days=14,
        knowledge_cutoff_at=cutoff,
        policy=ForecastPathPolicy(
            interval_probability=Decimal("0.80"),
            simulation_count=100,
            minimum_residual_samples=3,
            minimum_weekly_uncertainty=Decimal("10.00"),
            low_confidence_multiplier=Decimal("1.50"),
            stale_data_multiplier=Decimal("2.00"),
            random_seed=37,
            freshness=FreshnessPolicy(
                max_transaction_age_days=14,
                max_balance_age_days=14,
                max_coverage_age_days=14,
                minimum_contiguous_coverage_days=60,
            ),
        ),
    )
    return {
        "dataset_plan": dataset_plan.model_dump(mode="json"),
        "model_policy": model_policy.model_dump(mode="json"),
        "path_plan": path_plan.model_dump(mode="json"),
    }


def test_csv_to_forecast_flow_crosses_all_trusted_boundaries(
    integration_api: tuple[TestClient, AppContainer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, container = integration_api
    evidence_at = datetime(2026, 8, 30, 12, tzinfo=UTC)
    api_now = datetime(2026, 8, 31, 0, 0, 1, tzinfo=UTC)
    monkeypatch.setattr(
        "cashflow_ai.imports.csv_import_service.utc_now", lambda: evidence_at
    )
    monkeypatch.setattr(
        "cashflow_ai.financial_roles.service.utc_now", lambda: evidence_at
    )
    monkeypatch.setattr("cashflow_ai.recurrence.service.utc_now", lambda: evidence_at)
    monkeypatch.setattr("cashflow_ai.invalidation.service.utc_now", lambda: api_now)
    monkeypatch.setattr("cashflow_ai.api.decision_services.utc_now", lambda: api_now)
    profile_id, account_id = _create_profile_and_account(client)
    as_of_date = date(2026, 8, 30)
    content, start, closing_balance = _synthetic_year_csv(as_of_date)

    preview = client.post(
        "/api/v1/imports/csv/preview",
        files={"file": ("../fictional-year.csv", content, "text/csv")},
    )
    preview.raise_for_status()
    assert preview.json()["source_filename"] == "fictional-year.csv"
    plan = CsvImportPlan(
        account_id=account_id,
        statement_context=ImportContext(
            account_id=account_id,
            coverage=StatementCoverage(
                statement_start_date=start,
                statement_end_date=as_of_date,
                status=CoverageStatus.COMPLETE,
            ),
            balances=StatementBalances(
                opening_balance=Decimal("1000.00"),
                closing_balance=closing_balance,
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
        preview_file_hash=preview.json()["file_hash"],
        user_confirmed=True,
        confirmed_at=evidence_at,
    )
    imported = client.post(
        "/api/v1/imports/csv/confirm",
        files={"file": ("../fictional-year.csv", content, "text/csv")},
        data={
            "plan_json": plan.model_dump_json(),
            "confirmation_json": confirmation.model_dump_json(),
        },
    )
    imported.raise_for_status()
    transactions_response = client.get(
        f"/api/v1/accounts/{account_id}/transactions?limit=100"
    )
    transactions_response.raise_for_status()
    second_page = client.get(
        f"/api/v1/accounts/{account_id}/transactions?limit=100&offset=100"
    )
    second_page.raise_for_status()
    transactions = cast(
        list[dict[str, Any]],
        transactions_response.json()["items"] + second_page.json()["items"],
    )
    assert imported.json()["new_transactions"] == len(transactions)
    assert len(transactions) > 52
    assert all("raw_payload" not in transaction for transaction in transactions)
    with session_scope(container.session_factory) as session:
        raw_rows = tuple(
            session.scalars(
                select(RawTransactionRecord).order_by(
                    RawTransactionRecord.source_row_number
                )
            )
        )
    assert len(raw_rows) == len(transactions)
    assert raw_rows[0].raw_payload == {
        "Date": "2025-08-25",
        "Description": "SYNTHETIC SALARY",
        "Amount": "300.00",
        "Balance": "1300.00",
    }
    assert raw_rows[0].original_date_text == "2025-08-25"
    assert raw_rows[0].original_description == "SYNTHETIC SALARY"
    assert raw_rows[0].original_amount_text == "300.00"

    transaction_ids = tuple(
        cast(str, transaction["transaction_id"]) for transaction in transactions
    )
    rules = load_category_rule_set(
        Path("configs/category_rules.yaml"),
        load_taxonomy(Path("configs/categories.yaml")),
    )
    decisions = categorise_verified_transactions(
        container.session_factory,
        plan=CategorisationPlan(
            user_profile_id=profile_id,
            transaction_ids=transaction_ids,
        ),
        rule_set=rules,
    )
    assert {decision.category_id for decision in decisions} == {"groceries", "income"}

    for transaction in transactions:
        role = "income" if Decimal(transaction["amount"]) > 0 else "expense"
        role_response = client.post(
            f"/api/v1/transactions/{transaction['transaction_id']}/financial-role",
            json={"action": role, "changed_at": evidence_at.isoformat()},
        )
        role_response.raise_for_status()

    recurrence_cutoff = evidence_at
    recurrence = client.post(
        "/api/v1/recurring/detect?limit=100",
        json={
            "user_profile_id": profile_id,
            "as_of_date": (as_of_date - timedelta(days=1)).isoformat(),
            "knowledge_cutoff_at": recurrence_cutoff.isoformat(),
            "policy": {
                "minimum_occurrences": 3,
                "maximum_amount_variation": "1.00",
                "maximum_interval_variation_days": 4,
                "maximum_skipped_occurrences": 1,
                "minimum_confidence": 0.65,
            },
        },
    )
    assert recurrence.status_code == 200, recurrence.text
    income_candidate = next(
        item
        for item in recurrence.json()["items"]
        if item["financial_role"] == "income"
    )
    assert income_candidate["status"] == "pending"

    analytics = client.post(
        "/api/v1/analytics/cash-flow",
        json={
            "user_profile_id": profile_id,
            "account_ids": [account_id],
            "period": {
                "start_date": start.isoformat(),
                "end_date": as_of_date.isoformat(),
            },
            "view": "account",
        },
    )
    analytics.raise_for_status()
    analytics_body = analytics.json()
    assert analytics_body["coverage"]["status"] == "complete"
    assert Decimal(analytics_body["totals"]["total_income"]) > 0
    assert Decimal(analytics_body["totals"]["total_expenses"]) > 0

    forecast_payload = _forecast_payload(
        profile_id=profile_id,
        account_id=account_id,
        start=start,
        as_of_date=as_of_date,
        cutoff=datetime.combine(as_of_date, datetime.max.time(), tzinfo=UTC),
    )
    evaluation = client.post(
        "/api/v1/forecasts/evaluate",
        json={
            "dataset_plan": forecast_payload["dataset_plan"],
            "model_policy": forecast_payload["model_policy"],
        },
    )
    evaluation.raise_for_status()
    comparison = evaluation.json()["comparison"]
    assert comparison["training_sample_count"] == 0
    assert comparison["selected"] is False
    assert comparison["selected_model"] == "recent_rolling_mean"
    assert comparison["selection_reason"] == (
        "Insufficient complete consecutive weeks; use the recent-mean fallback."
    )
    forecast = client.post("/api/v1/forecasts/balance", json=forecast_payload)
    assert forecast.status_code == 200, forecast.text
    assert len(forecast.json()["daily_balances"]) == 14
    assert forecast.json()["opening_balance"]["balance"] == f"{closing_balance:.2f}"

    freshness = client.get(f"/api/v1/accounts/{account_id}/derived-freshness")
    freshness.raise_for_status()
    states = {item["output_type"]: item["status"] for item in freshness.json()["items"]}
    assert states["analytics"] == "current"
    assert states["forecasts"] == "current"
    assert states["model_performance_comparisons"] == "current"


def _finish_pdf(document: Any) -> bytes:
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _scanned_pdf() -> bytes:
    image = Image.new("RGB", (240, 320), "white")
    drawing = ImageDraw.Draw(image)
    drawing.text((15, 20), "Fictional scanned statement", fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=240, height=320)
    page.insert_image(page.rect, stream=output.getvalue())  # type: ignore[no-untyped-call]
    return _finish_pdf(document)


def test_scanned_pdf_correction_preserves_evidence_and_downstream_gate(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ocr_engine = DecimalErrorOcrEngine()
    container = _container(tmp_path, ocr_engine=ocr_engine)
    content = _scanned_pdf()
    with TestClient(create_app(container)) as client:
        profile_id, account_id = _create_profile_and_account(client)
        preview = client.post(
            "/api/v1/imports/pdf/ocr/preview",
            files={
                "file": (
                    "../../PRIVATE-SYNTHETIC-ACCOUNT.pdf",
                    content,
                    "application/pdf",
                )
            },
            data={"account_id": account_id, "account_currency": "GBP"},
        )
        preview.raise_for_status()
        assert preview.json()["source_filename"] == "PRIVATE-SYNTHETIC-ACCOUNT.pdf"
        review = client.post(
            "/api/v1/imports/pdf/review",
            files={
                "file": (
                    "../../PRIVATE-SYNTHETIC-ACCOUNT.pdf",
                    content,
                    "application/pdf",
                )
            },
            data={
                "source_type": "ocr_pdf",
                "account_id": account_id,
                "account_currency": "GBP",
                "ocr_confidence_threshold": "0.85",
            },
        )
        review.raise_for_status()
        body = review.json()
        row = body["rows"][0]
        assert body["requires_date_format_confirmation"] is True
        assert row["extracted_draft"]["amount"] == "-450.00"
        assert row["review_reasons"] == ["low_ocr_confidence"]
        corrected = dict(row["working_draft"])
        corrected["amount"] = "-4.50"
        approval = {
            "file_hash": body["file_hash"],
            "approved_at": datetime.now(UTC).isoformat(),
            "statement_approved": True,
            "date_format": "day_first",
            "confirmed_statement_coverage": body["statement_coverage"],
            "confirmed_balances": body["balances"],
            "row_reviews": [
                {
                    "source_fingerprint": row["source_fingerprint"],
                    "decision": "confirm",
                    "corrected_draft": corrected,
                }
            ],
        }
        approved = client.post(
            "/api/v1/imports/pdf/confirm",
            files={
                "file": (
                    "../../PRIVATE-SYNTHETIC-ACCOUNT.pdf",
                    content,
                    "application/pdf",
                )
            },
            data={
                "source_type": "ocr_pdf",
                "account_id": account_id,
                "account_currency": "GBP",
                "ocr_confidence_threshold": "0.85",
                "approval_json": json.dumps(approval),
            },
        )
        approved.raise_for_status()
        approved_row = approved.json()["rows"][0]
        assert approved_row["original"]["signed_amount_text"] == "-450"
        assert approved_row["extracted_draft"]["amount"] == "-450.00"
        assert approved_row["transaction"]["amount"] == "-4.50"
        assert approved_row["was_edited"] is True
        assert approved.json()["reconciliation"]["status"] == "reconciled"

        transactions = client.get(f"/api/v1/accounts/{account_id}/transactions")
        transactions.raise_for_status()
        assert transactions.json()["total"] == 0
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
        analytics.raise_for_status()
        assert analytics.json()["coverage"]["status"] == "missing"
        assert analytics.json()["totals"] is None

    assert ocr_engine.processed_images
    for image in ocr_engine.processed_images:
        with pytest.raises(ValueError, match="closed image"):
            image.getpixel((0, 0))
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "PRIVATE-SYNTHETIC-ACCOUNT" not in logs

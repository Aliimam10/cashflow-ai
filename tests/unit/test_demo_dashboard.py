"""Tests for the isolated, explicitly labelled synthetic dashboard loader."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.api.services import search_transactions as search_api_transactions
from cashflow_ai.demo_data import dashboard
from cashflow_ai.demo_data.dashboard import (
    SyntheticDashboardError,
    SyntheticDashboardSummary,
    seed_synthetic_dashboard,
)
from cashflow_ai.imports import persist_confirmed_csv as persist_csv
from cashflow_ai.persistence import (
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    CategoryCorrectionRecord,
    CategoryRecord,
    FinancialRoleAuditRecord,
    FinancialRoleRecord,
    RawTransactionRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.transactions import FinancialRole

_CONTENT = (
    b"transaction_date,posting_date,description,amount,balance,currency,"
    b"external_id,transaction_type,category,category_id,financial_role\n"
    b"2026-01-01,2026-01-02,SYNTHETIC FUNDING,1000.00,1000.00,GBP,"
    b"SYN-1,credit,Income,income,income\n"
    b"2026-01-02,2026-01-03,SYNTHETIC RENT,-100.00,900.00,GBP,"
    b"SYN-2,direct_debit,Housing,housing,expense\n"
)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    built = create_session_factory(engine)
    with session_scope(built) as session:
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            (
                CategoryRecord(
                    id="income",
                    name="Income",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
                CategoryRecord(
                    id="housing",
                    name="Housing",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
            )
        )
    return built


def test_seed_imports_and_audits_every_synthetic_label(
    factory: sessionmaker[Session],
) -> None:
    result = seed_synthetic_dashboard(factory, _CONTENT)

    assert result == SyntheticDashboardSummary(
        imported_transactions=2,
        labelled_transactions=2,
        statement_start_date=date(2026, 1, 1),
        statement_end_date=date(2026, 1, 2),
        total_income=Decimal("1000.00"),
        total_expenses=Decimal("100.00"),
    )
    with session_scope(factory) as session:
        transactions = session.scalars(
            select(VerifiedTransactionRecord).order_by(
                VerifiedTransactionRecord.external_id
            )
        ).all()
        assert [item.financial_role_id for item in transactions] == [
            "income",
            "expense",
        ]
        assert [item.category_id for item in transactions] == ["income", "housing"]
        assert (
            session.scalar(select(func.count()).select_from(FinancialRoleAuditRecord))
            == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(CategoryCorrectionRecord))
            == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 2
        )

    with pytest.raises(SyntheticDashboardError, match="already contains a profile"):
        seed_synthetic_dashboard(factory, _CONTENT)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff", "must use UTF-8"),
        (b"external_id,category_id\nSYN-1,income\n", "missing its explicit"),
        (
            b"external_id,category_id,financial_role\nSYN-1,income,refund\n",
            "unsupported or empty",
        ),
        (
            b"external_id,category_id,financial_role\n"
            b"SYN-1,income,income\nSYN-1,housing,expense\n",
            "conflicting duplicate",
        ),
        (b"external_id,category_id,financial_role\n", "no labelled rows"),
    ],
)
def test_fixture_label_contract_rejects_unsafe_demo_inputs(
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(SyntheticDashboardError, match=message):
        dashboard._read_fixture_labels(content)


def test_seed_rejects_unknown_categories_and_unreadable_dates_before_writes(
    factory: sessionmaker[Session],
) -> None:
    unknown_category = _CONTENT.replace(b"Housing,housing", b"Unknown,unknown")
    with pytest.raises(SyntheticDashboardError, match="active taxonomy"):
        seed_synthetic_dashboard(factory, unknown_category)

    invalid_date = (
        b"transaction_date,posting_date,description,amount,balance,currency,"
        b"external_id,transaction_type,category,category_id,financial_role\n"
        b"not-a-date,2026-01-02,SYNTHETIC FUNDING,1000.00,1000.00,GBP,"
        b"SYN-1,credit,Income,income,income\n"
        b"also-invalid,2026-01-03,SYNTHETIC RENT,-100.00,900.00,GBP,"
        b"SYN-2,direct_debit,Housing,housing,expense\n"
    )
    with pytest.raises(SyntheticDashboardError, match="no readable"):
        seed_synthetic_dashboard(factory, invalid_date)


def test_seed_refuses_a_non_demo_file_database(tmp_path: Path) -> None:
    unsafe = create_session_factory(
        create_sqlite_engine(f"sqlite+pysqlite:///{tmp_path / 'cashflow.db'}")
    )

    with pytest.raises(SyntheticDashboardError, match="refusing demo writes"):
        seed_synthetic_dashboard(unsafe, _CONTENT)


def test_seed_refuses_to_claim_success_when_import_quarantines_a_labelled_row(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def report_rejection(*args: Any, **kwargs: Any) -> object:
        result = persist_csv(*args, **kwargs)
        return result.model_copy(update={"rejected_rows": 1})

    monkeypatch.setattr(dashboard, "persist_confirmed_csv", report_rejection)

    with pytest.raises(SyntheticDashboardError, match="quarantined"):
        seed_synthetic_dashboard(factory, _CONTENT)


def test_seed_detects_missing_post_import_labels_and_incomplete_totals(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_search = search_api_transactions

    def missing_label(*args: object, **kwargs: object) -> tuple[object, ...]:
        transactions = real_search(*args, **kwargs)  # type: ignore[arg-type]
        return (transactions[0].model_copy(update={"external_id": "missing"}),)

    monkeypatch.setattr(dashboard, "search_transactions", missing_label)
    with pytest.raises(SyntheticDashboardError, match="do not match every"):
        seed_synthetic_dashboard(factory, _CONTENT)

    second_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(second_engine)
    second_factory = create_session_factory(second_engine)
    with session_scope(second_factory) as session:
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            (
                CategoryRecord(
                    id="income",
                    name="Income",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
                CategoryRecord(
                    id="housing",
                    name="Housing",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
            )
        )
    monkeypatch.setattr(dashboard, "search_transactions", real_search)
    monkeypatch.setattr(
        dashboard,
        "compute_cash_flow_analytics",
        MagicMock(return_value=SimpleNamespace(totals=None)),
    )
    with pytest.raises(SyntheticDashboardError, match="complete cash-flow totals"):
        seed_synthetic_dashboard(second_factory, _CONTENT)


def test_demo_database_guard_and_command_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert dashboard._is_isolated_demo_database(
        "sqlite:///data/cashflow-demo.db?mode=rwc"
    )
    assert not dashboard._is_isolated_demo_database("sqlite:///data/cashflow.db")

    monkeypatch.setattr(
        dashboard,
        "load_settings",
        MagicMock(
            return_value=SimpleNamespace(database_url="sqlite:///data/cashflow.db")
        ),
    )
    with pytest.raises(SyntheticDashboardError, match="refusing demo writes"):
        dashboard.main()

    demo_settings = SimpleNamespace(database_url="sqlite:///data/cashflow-demo.db")
    monkeypatch.setattr(
        dashboard, "load_settings", MagicMock(return_value=demo_settings)
    )
    missing = tmp_path / "missing.csv"
    monkeypatch.setattr(dashboard, "DEFAULT_DEMO_CSV", missing)
    with pytest.raises(SyntheticDashboardError, match="generate the synthetic"):
        dashboard.main()

    fixture = tmp_path / "student_canonical.csv"
    fixture.write_bytes(_CONTENT)
    monkeypatch.setattr(dashboard, "DEFAULT_DEMO_CSV", fixture)
    factory_sentinel = object()
    monkeypatch.setattr(
        dashboard,
        "build_container",
        MagicMock(return_value=SimpleNamespace(session_factory=factory_sentinel)),
    )
    expected = SyntheticDashboardSummary(
        imported_transactions=2,
        labelled_transactions=2,
        statement_start_date=date(2026, 1, 1),
        statement_end_date=date(2026, 1, 2),
        total_income=Decimal("1000.00"),
        total_expenses=Decimal("100.00"),
    )
    seed = MagicMock(return_value=expected)
    monkeypatch.setattr(dashboard, "seed_synthetic_dashboard", seed)

    assert dashboard.main() == 0
    seed.assert_called_once_with(factory_sentinel, _CONTENT, filename=fixture.name)
    output = capsys.readouterr().out
    assert "CashFlow AI synthetic dashboard ready" in output
    assert "2026-01-01 to 2026-01-02" in output
    assert "financial roles and categories approved: 2" in output
    assert "income: GBP 1000.00" in output
    assert "expenses: GBP 100.00" in output

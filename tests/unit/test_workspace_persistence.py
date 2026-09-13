"""Tests for data-minimised saved-workspace persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import (
    Base,
    SavedWorkspaceRecords,
    SavedWorkspaceRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    SavedWorkspaceBalanceRecord,
    SavedWorkspaceCoverageRecord,
    SavedWorkspaceRecord,
    SavedWorkspaceTransactionRecord,
)

FINALIZED_AT = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def workspace_record(
    *,
    workspace_id: str = "workspace-1",
    retention_mode: str = "saved",
    status: str = "finalized",
    revision: int = 1,
    finalized_at: datetime = FINALIZED_AT,
) -> SavedWorkspaceRecord:
    return SavedWorkspaceRecord(
        id=workspace_id,
        account_name="Fictional current account",
        retention_mode=retention_mode,
        status=status,
        revision=revision,
        created_at=FINALIZED_AT,
        finalized_at=finalized_at,
    )


def transaction_record(
    *,
    row_id: str = "row-1",
    workspace_id: str = "workspace-1",
    position: int = 1,
    amount: Decimal = Decimal("-12.50"),
    direction: str = "outflow",
) -> SavedWorkspaceTransactionRecord:
    return SavedWorkspaceTransactionRecord(
        id=row_id,
        workspace_id=workspace_id,
        position=position,
        account_id="fictional-current-account",
        transaction_date=date(2026, 8, position),
        posting_date=None,
        description=f"Fictional merchant {position}",
        merchant=f"Fictional merchant {position}",
        amount=amount,
        balance_after=Decimal("987.50"),
        currency="GBP",
        external_id=None,
        transaction_type="card",
        direction=direction,
        category_id="groceries",
        financial_role="expense",
    )


def saved_records(
    *,
    workspace: SavedWorkspaceRecord | None = None,
    transactions: tuple[SavedWorkspaceTransactionRecord, ...] | None = None,
    include_evidence: bool = True,
) -> SavedWorkspaceRecords:
    selected_workspace = workspace or workspace_record()
    selected_transactions = (
        (transaction_record(),) if transactions is None else transactions
    )
    coverage = None
    balance = None
    if include_evidence:
        coverage = SavedWorkspaceCoverageRecord(
            workspace_id=selected_workspace.id,
            statement_start_date=date(2026, 8, 1),
            statement_end_date=date(2026, 8, 31),
            coverage_status="gapped",
            missing_periods_json=[
                {"start_date": "2026-08-10", "end_date": "2026-08-11"}
            ],
        )
        balance = SavedWorkspaceBalanceRecord(
            workspace_id=selected_workspace.id,
            balance=Decimal("987.50"),
            currency="GBP",
            as_of_date=date(2026, 8, 31),
        )
    return SavedWorkspaceRecords(
        workspace=selected_workspace,
        transactions=selected_transactions,
        coverage=coverage,
        balance=balance,
    )


def test_repository_saves_loads_and_revision_replaces_canonical_snapshot(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        repository = SavedWorkspaceRepository(session)
        assert repository.get("missing") is None
        assert repository.get_latest() is None
        records = saved_records(
            transactions=(
                transaction_record(row_id="row-2", position=2),
                transaction_record(),
            )
        )
        stored = repository.save(records)
        assert stored.workspace is records.workspace

    with session_scope(factory) as session:
        repository = SavedWorkspaceRepository(session)
        restored = repository.get("workspace-1")
        assert restored is not None
        assert tuple(row.id for row in restored.transactions) == ("row-1", "row-2")
        assert restored.transactions[0].amount == Decimal("-12.50")
        assert restored.workspace.finalized_at.tzinfo is UTC
        assert restored.workspace.account_name == "Fictional current account"
        assert restored.coverage is not None
        assert restored.coverage.missing_periods_json == [
            {"start_date": "2026-08-10", "end_date": "2026-08-11"}
        ]
        assert restored.balance is not None
        assert restored.balance.balance == Decimal("987.50")

        updated = saved_records(
            workspace=workspace_record(
                revision=2, finalized_at=FINALIZED_AT + timedelta(days=1)
            ),
            transactions=(transaction_record(row_id="replacement-row"),),
        )
        replaced = repository.save(updated)
        assert replaced.workspace.created_at == FINALIZED_AT

        with pytest.raises(ValueError, match="advance exactly once"):
            repository.save(updated)

    with session_scope(factory) as session:
        restored = SavedWorkspaceRepository(session).get("workspace-1")
        assert restored is not None
        assert restored.workspace.revision == 2
        assert tuple(row.id for row in restored.transactions) == ("replacement-row",)
        assert restored.coverage is not None
        assert restored.balance is not None


def test_latest_and_delete_are_deterministic_and_cascade(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        repository = SavedWorkspaceRepository(session)
        repository.save(saved_records())
        second_workspace = workspace_record(
            workspace_id="workspace-2",
            finalized_at=FINALIZED_AT + timedelta(days=1),
        )
        repository.save(
            saved_records(
                workspace=second_workspace,
                transactions=(
                    transaction_record(
                        row_id="workspace-2-row",
                        workspace_id="workspace-2",
                    ),
                ),
            )
        )
        latest = repository.get_latest()
        assert latest is not None
        assert latest.workspace.id == "workspace-2"
        assert repository.delete("missing") is False
        assert repository.delete("workspace-2") is True

    with session_scope(factory) as session:
        repository = SavedWorkspaceRepository(session)
        latest = repository.get_latest()
        assert latest is not None
        assert latest.workspace.id == "workspace-1"
        assert repository.get("workspace-2") is None
        child_count = session.scalar(
            select(func.count())
            .select_from(SavedWorkspaceTransactionRecord)
            .where(SavedWorkspaceTransactionRecord.workspace_id == "workspace-2")
        )
        assert child_count == 0


def test_delete_all_removes_every_saved_workspace_and_child(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        repository = SavedWorkspaceRepository(session)
        repository.save(saved_records())
        second = workspace_record(workspace_id="workspace-2")
        repository.save(
            saved_records(
                workspace=second,
                transactions=(
                    transaction_record(
                        row_id="workspace-2-row",
                        workspace_id="workspace-2",
                    ),
                ),
            )
        )
        assert repository.delete_all() == 2
        assert repository.delete_all() == 0

    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(SavedWorkspaceRecord)) == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(SavedWorkspaceTransactionRecord)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("records", "message"),
    [
        (
            saved_records(
                workspace=workspace_record(retention_mode="temporary"),
                include_evidence=False,
            ),
            "temporary workspaces must remain in memory",
        ),
        (
            saved_records(
                workspace=workspace_record(status="reviewing"),
                include_evidence=False,
            ),
            "only finalized workspaces may be saved",
        ),
        (
            saved_records(
                transactions=(transaction_record(workspace_id="other-workspace"),),
            ),
            "children must reference their parent",
        ),
        (
            saved_records(transactions=(), include_evidence=True),
            "at least one canonical transaction",
        ),
        (
            saved_records(include_evidence=False),
            "confirmed coverage",
        ),
    ],
)
def test_repository_rejects_non_saved_or_incoherent_records(
    factory: sessionmaker[Session],
    records: SavedWorkspaceRecords,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message), session_scope(factory) as session:
        SavedWorkspaceRepository(session).save(records)

    with session_scope(factory) as session:
        count = session.scalar(select(func.count()).select_from(SavedWorkspaceRecord))
        assert count == 0


def test_database_constraints_reject_temporary_and_invalid_canonical_rows(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.add(workspace_record(retention_mode="temporary"))

    with session_scope(factory) as session:
        session.add(workspace_record())

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.add(
            transaction_record(
                row_id="invalid-sign-row",
                amount=Decimal("12.50"),
                direction="outflow",
            )
        )


def test_saved_schema_has_no_uploaded_source_storage(engine: Engine) -> None:
    inspector = inspect(engine)
    columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in (
            "saved_workspaces",
            "saved_workspace_transactions",
            "saved_workspace_coverage",
            "saved_workspace_balances",
        )
    }
    all_columns = set().union(*columns.values())

    forbidden = {
        "bytes",
        "content",
        "file_hash",
        "filename",
        "mime_type",
        "page_number",
        "pdf_text",
        "raw_payload",
        "source_filename",
        "source_row_number",
    }
    assert all_columns.isdisjoint(forbidden)
    assert columns["saved_workspace_transactions"] >= {
        "transaction_date",
        "description",
        "amount",
        "category_id",
        "financial_role",
    }


def test_confirmed_coverage_can_be_saved_without_optional_balance(
    factory: sessionmaker[Session],
) -> None:
    records = saved_records()
    without_balance = SavedWorkspaceRecords(
        workspace=records.workspace,
        transactions=records.transactions,
        coverage=records.coverage,
        balance=None,
    )
    with session_scope(factory) as session:
        SavedWorkspaceRepository(session).save(without_balance)
    with session_scope(factory) as session:
        restored = SavedWorkspaceRepository(session).get("workspace-1")
        assert restored is not None
        assert restored.coverage is not None
        assert restored.balance is None

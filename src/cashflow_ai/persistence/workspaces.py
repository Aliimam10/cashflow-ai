"""Data-minimised persistence helpers for finalized statement workspaces."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from cashflow_ai.persistence.models import (
    SavedWorkspaceBalanceRecord,
    SavedWorkspaceCoverageRecord,
    SavedWorkspaceRecord,
    SavedWorkspaceTransactionRecord,
)


@dataclass(frozen=True, slots=True)
class SavedWorkspaceRecords:
    """Complete persisted projection without any uploaded-source material."""

    workspace: SavedWorkspaceRecord
    transactions: tuple[SavedWorkspaceTransactionRecord, ...]
    coverage: SavedWorkspaceCoverageRecord | None = None
    balance: SavedWorkspaceBalanceRecord | None = None


class SavedWorkspaceRepository:
    """Atomically save and load finalized canonical workspace snapshots."""

    def __init__(self, session: Session) -> None:
        """Bind persistence operations to one caller-owned transaction."""
        self._session = session

    def save(self, records: SavedWorkspaceRecords) -> SavedWorkspaceRecords:
        """Insert or revision-replace one saved, finalized workspace."""
        coverage = self._validate_records(records)
        incoming = records.workspace
        current = self._session.get(SavedWorkspaceRecord, incoming.id)
        if current is None:
            self._session.add(incoming)
            self._session.flush()
            persisted_workspace = incoming
        else:
            if incoming.revision != current.revision + 1:
                msg = "saved workspace revision must advance exactly once"
                raise ValueError(msg)
            current.revision = incoming.revision
            current.account_name = incoming.account_name
            current.finalized_at = incoming.finalized_at
            persisted_workspace = current
            self._delete_children(incoming.id)
            self._session.flush()

        self._session.add_all(records.transactions)
        self._session.add(coverage)
        if records.balance is not None:
            self._session.add(records.balance)
        self._session.flush()
        return SavedWorkspaceRecords(
            workspace=persisted_workspace,
            transactions=records.transactions,
            coverage=records.coverage,
            balance=records.balance,
        )

    def get(self, workspace_id: str) -> SavedWorkspaceRecords | None:
        """Load one saved workspace and its canonical financial evidence."""
        workspace = self._session.get(SavedWorkspaceRecord, workspace_id)
        if workspace is None:
            return None
        return self._load_records(workspace)

    def get_latest(self) -> SavedWorkspaceRecords | None:
        """Load the most recently finalized saved workspace, when present."""
        statement = select(SavedWorkspaceRecord).order_by(
            desc(SavedWorkspaceRecord.finalized_at),
            desc(SavedWorkspaceRecord.id),
        )
        workspace = self._session.scalars(statement).first()
        if workspace is None:
            return None
        return self._load_records(workspace)

    def delete(self, workspace_id: str) -> bool:
        """Delete one saved workspace and its canonical children."""
        workspace = self._session.get(SavedWorkspaceRecord, workspace_id)
        if workspace is None:
            return False
        self._session.delete(workspace)
        self._session.flush()
        return True

    def delete_all(self) -> int:
        """Delete every saved statement workspace and its cascading children."""
        workspace_ids = tuple(self._session.scalars(select(SavedWorkspaceRecord.id)))
        if workspace_ids:
            self._session.execute(delete(SavedWorkspaceRecord))
            self._session.flush()
        return len(workspace_ids)

    def _load_records(self, workspace: SavedWorkspaceRecord) -> SavedWorkspaceRecords:
        transactions = tuple(
            self._session.scalars(
                select(SavedWorkspaceTransactionRecord)
                .where(SavedWorkspaceTransactionRecord.workspace_id == workspace.id)
                .order_by(SavedWorkspaceTransactionRecord.position)
            )
        )
        return SavedWorkspaceRecords(
            workspace=workspace,
            transactions=transactions,
            coverage=self._session.get(SavedWorkspaceCoverageRecord, workspace.id),
            balance=self._session.get(SavedWorkspaceBalanceRecord, workspace.id),
        )

    def _delete_children(self, workspace_id: str) -> None:
        self._session.execute(
            delete(SavedWorkspaceTransactionRecord).where(
                SavedWorkspaceTransactionRecord.workspace_id == workspace_id
            )
        )
        self._session.execute(
            delete(SavedWorkspaceCoverageRecord).where(
                SavedWorkspaceCoverageRecord.workspace_id == workspace_id
            )
        )
        self._session.execute(
            delete(SavedWorkspaceBalanceRecord).where(
                SavedWorkspaceBalanceRecord.workspace_id == workspace_id
            )
        )

    @staticmethod
    def _validate_records(
        records: SavedWorkspaceRecords,
    ) -> SavedWorkspaceCoverageRecord:
        workspace = records.workspace
        if workspace.retention_mode != "saved":
            msg = "temporary workspaces must remain in memory"
            raise ValueError(msg)
        if workspace.status != "finalized":
            msg = "only finalized workspaces may be saved"
            raise ValueError(msg)
        if not records.transactions:
            msg = "saved workspaces require at least one canonical transaction"
            raise ValueError(msg)
        coverage = records.coverage
        if coverage is None:
            msg = "saved workspaces require confirmed coverage"
            raise ValueError(msg)
        child_workspace_ids = {
            transaction.workspace_id for transaction in records.transactions
        }
        child_workspace_ids.add(coverage.workspace_id)
        if records.balance is not None:
            child_workspace_ids.add(records.balance.workspace_id)
        if child_workspace_ids - {workspace.id}:
            msg = "saved workspace children must reference their parent"
            raise ValueError(msg)
        return coverage

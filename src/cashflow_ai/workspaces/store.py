"""Thread-safe process-memory storage for active statement workspaces."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from cashflow_ai.schemas.workspaces import StatementWorkspace


class WorkspaceStore:
    """Keep draft and temporary workspaces out of browser state and SQLite."""

    def __init__(self) -> None:
        """Create an empty process-local store."""
        self._items: dict[str, StatementWorkspace] = {}
        self._lock = RLock()

    def add(self, workspace: StatementWorkspace) -> StatementWorkspace:
        """Add a new workspace without replacing an existing identity."""
        with self._lock:
            if workspace.workspace_id in self._items:
                raise ValueError("workspace identity already exists")
            self._items[workspace.workspace_id] = workspace
            return workspace

    def get(self, workspace_id: str) -> StatementWorkspace | None:
        """Return one immutable workspace snapshot."""
        with self._lock:
            return self._items.get(workspace_id)

    def replace(
        self,
        workspace: StatementWorkspace,
        *,
        expected_revision: int,
    ) -> StatementWorkspace:
        """Optimistically replace one workspace snapshot."""
        with self._lock:
            current = self._items.get(workspace.workspace_id)
            if current is None:
                raise KeyError(workspace.workspace_id)
            if current.revision != expected_revision:
                raise RuntimeError("workspace revision changed")
            if workspace.revision != expected_revision + 1:
                raise ValueError("replacement revision must advance exactly once")
            self._items[workspace.workspace_id] = workspace
            return workspace

    @contextmanager
    def locked(self, workspace_id: str) -> Iterator[StatementWorkspace | None]:
        """Hold the store lock while one cross-boundary mutation is completed.

        This is intentionally narrow: callers receive only the current immutable
        snapshot and must still use :meth:`replace` for revision validation.  The
        re-entrant lock lets that replacement happen without allowing a competing
        edit between persistence and the in-memory compare-and-swap.
        """
        with self._lock:
            yield self._items.get(workspace_id)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize a store/database operation that may affect many identities."""
        with self._lock:
            yield

    def delete(self, workspace_id: str) -> bool:
        """Remove active in-memory state and report whether it existed."""
        with self._lock:
            return self._items.pop(workspace_id, None) is not None

    def count(self) -> int:
        """Return the number of active process-memory workspaces."""
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """Discard every process-local workspace during application shutdown."""
        with self._lock:
            self._items.clear()


__all__ = ["WorkspaceStore"]

"""Readable proof for the entirely fictional statement-workspace demo."""

import pytest

from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.workspaces.demo import _confirm_every_row, _finalize, main
from cashflow_ai.workspaces.store import WorkspaceStore


def test_workspace_demo_proves_mixed_saved_and_temporary_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main()
    output = capsys.readouterr().out

    assert output.splitlines() == [
        "CashFlow AI synthetic statement-workspace check",
        "mixed sources accepted: 2",
        "combined canonical rows: 3",
        "saved rows restored: 3",
        "original CSV/PDF bytes persisted: no",
        "temporary workspace persisted: no",
        "temporary workspace in memory after API-stop cleanup: no",
    ]


def test_workspace_demo_guards_against_an_unexpectedly_missing_workspace() -> None:
    store = WorkspaceStore()
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    with pytest.raises(RuntimeError, match="workspace unexpectedly disappeared"):
        _confirm_every_row(store, "missing-workspace")
    with pytest.raises(RuntimeError, match="workspace unexpectedly disappeared"):
        _finalize(store, "missing-workspace", factory, balance=None)

    engine.dispose()

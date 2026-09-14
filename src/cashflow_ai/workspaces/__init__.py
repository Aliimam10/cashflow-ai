"""Session-based multi-statement workspace boundary."""

from cashflow_ai.workspaces.analytics import (
    WorkspaceAnalyticsError,
    WorkspaceAnalyticsErrorCode,
    compute_workspace_analytics,
    search_workspace_transactions,
)
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace
from cashflow_ai.workspaces.service import (
    MAX_WORKSPACE_FILES,
    MAX_WORKSPACE_UPLOAD_BYTES,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceUpload,
    create_workspace,
    delete_all_workspace_data,
    delete_workspace,
    edit_workspace_rows,
    finalize_workspace,
    get_latest_saved_workspace,
    get_workspace,
    remove_workspace_source,
    review_workspace_uploads,
    workspace_csv_download,
)
from cashflow_ai.workspaces.store import WorkspaceStore

__all__ = [
    "MAX_WORKSPACE_FILES",
    "MAX_WORKSPACE_UPLOAD_BYTES",
    "WorkspaceAnalyticsError",
    "WorkspaceAnalyticsErrorCode",
    "WorkspaceError",
    "WorkspaceErrorCode",
    "WorkspaceStore",
    "WorkspaceUpload",
    "compute_workspace_analytics",
    "create_workspace",
    "delete_all_workspace_data",
    "delete_workspace",
    "edit_workspace_rows",
    "finalize_workspace",
    "forecast_statement_workspace",
    "get_latest_saved_workspace",
    "get_workspace",
    "remove_workspace_source",
    "review_workspace_uploads",
    "search_workspace_transactions",
    "workspace_csv_download",
]

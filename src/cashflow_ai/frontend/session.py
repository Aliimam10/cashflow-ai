"""Data-minimised Streamlit session state."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from cashflow_ai.frontend.navigation import PageId
from cashflow_ai.schemas.transactions import Identifier

SESSION_KEY = "cashflow_ai_ui"


class SessionStore(Protocol):
    """Minimal mapping behaviour shared by dicts and Streamlit session state."""

    def get(self, key: str) -> object | None:
        """Return a stored value when present."""
        ...

    def __setitem__(self, key: str, value: object) -> None:
        """Store one data-minimised value."""
        ...


class FrontendSessionState(BaseModel):
    """Only UI selections safe to retain across Streamlit reruns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_page: PageId = PageId.HOME
    user_profile_id: Identifier | None = None
    account_id: Identifier | None = None
    privacy_notice_seen: bool = False


def load_session_state(state: SessionStore) -> FrontendSessionState:
    """Return valid state, replacing corrupt browser-session metadata safely."""
    raw = state.get(SESSION_KEY)
    try:
        current = FrontendSessionState.model_validate(raw or {})
    except (TypeError, ValueError):
        current = FrontendSessionState()
    state[SESSION_KEY] = current.model_dump(mode="json")
    return current


def save_session_state(state: SessionStore, value: FrontendSessionState) -> None:
    """Persist only validated identifiers and display preferences."""
    state[SESSION_KEY] = value.model_dump(mode="json")


__all__ = [
    "SESSION_KEY",
    "FrontendSessionState",
    "SessionStore",
    "load_session_state",
    "save_session_state",
]

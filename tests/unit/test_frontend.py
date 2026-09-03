"""Tests for Streamlit navigation, display states, and the packaged launcher."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.app as app
import cashflow_ai.frontend.cli as cli
import cashflow_ai.frontend.components as components
from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.frontend.client import ApiClientError, ApiClientErrorCode
from cashflow_ai.frontend.navigation import (
    NAVIGATION_ITEMS,
    PageId,
    navigation_item,
)
from cashflow_ai.frontend.session import (
    SESSION_KEY,
    FrontendSessionState,
    load_session_state,
    save_session_state,
)
from cashflow_ai.schemas.api import HealthResponse, ReadinessResponse


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        api_host="127.0.0.1",
        api_port=8765,
        ui_host="127.0.0.1",
        ui_port=8766,
    )


class StubStatusApi:
    """Small typed status double for readable home-page tests."""

    def __init__(self, *, ready: bool = True, error: ApiClientError | None = None):
        self.ready = ready
        self.error = error

    def health(self) -> HealthResponse:
        if self.error is not None:
            raise self.error
        return HealthResponse(version="0.1.0")

    def readiness(self) -> ReadinessResponse:
        return ReadinessResponse(
            status="ready" if self.ready else "not_ready",
            database_connection=True,
            database_schema=self.ready,
        )


def test_navigation_metadata_and_data_minimised_session_state() -> None:
    state: dict[str, object] = {}

    default = load_session_state(state)
    chosen = FrontendSessionState(
        selected_page=PageId.TRANSACTIONS,
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        privacy_notice_seen=True,
    )
    save_session_state(state, chosen)

    assert default == FrontendSessionState()
    assert state[SESSION_KEY] == chosen.model_dump(mode="json")
    assert load_session_state(state) == chosen
    assert tuple(navigation_item(item.page_id) for item in NAVIGATION_ITEMS) == (
        NAVIGATION_ITEMS
    )
    assert app.selected_navigation_item("Import statements").page_id is PageId.IMPORT


@pytest.mark.parametrize("invalid", [{"unexpected": "value"}, 42])
def test_invalid_session_metadata_is_replaced(invalid: object) -> None:
    state = {SESSION_KEY: invalid}

    assert load_session_state(state) == FrontendSessionState()
    assert state[SESSION_KEY] == FrontendSessionState().model_dump(mode="json")


def test_common_display_states_use_only_controlled_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = MagicMock()
    ui.spinner.return_value = nullcontext()
    monkeypatch.setattr(components, "st", ui)
    error = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "the local API is unavailable; start it and try again",
    )

    with components.loading_state("Checking synthetic service…"):
        pass
    components.render_error(error)
    components.render_error(
        ApiClientError(
            ApiClientErrorCode.API_REJECTED_REQUEST,
            "the local API rejected the request",
            problem_code="synthetic_problem",
        )
    )
    components.render_empty_state("No fictional rows", "Import synthetic data.")
    components.render_privacy_notice()
    components.render_forecast_disclaimer()

    ui.spinner.assert_called_once_with("Checking synthetic service…")
    assert ui.error.call_args_list[0].args == (
        "the local API is unavailable; start it and try again · "
        "code: `connection_failed`",
    )
    assert ui.error.call_args_list[1].args == (
        "the local API rejected the request · "
        "code: `api_rejected_request/synthetic_problem`",
    )
    assert ui.info.call_count == 2
    ui.warning.assert_called_once()


@pytest.mark.parametrize("ready", [True, False])
def test_home_renders_backend_status(
    ready: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = MagicMock()
    first = MagicMock()
    second = MagicMock()
    ui.columns.return_value = (first, second)
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(app, "loading_state", lambda message: nullcontext())
    privacy = MagicMock()
    disclaimer = MagicMock()
    monkeypatch.setattr(app, "render_privacy_notice", privacy)
    monkeypatch.setattr(app, "render_forecast_disclaimer", disclaimer)

    app.render_home(StubStatusApi(ready=ready))

    ui.title.assert_called_once_with("CashFlow AI")
    first.metric.assert_called_once_with("API", "ok")
    second.metric.assert_called_once_with("Database", "ready" if ready else "not ready")
    if ready:
        ui.success.assert_called_once()
        ui.warning.assert_not_called()
    else:
        ui.warning.assert_called_once()
        ui.success.assert_not_called()
    privacy.assert_called_once_with()
    disclaimer.assert_called_once_with()


def test_home_displays_safe_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = MagicMock()
    display_error = MagicMock()
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(app, "loading_state", lambda message: nullcontext())
    monkeypatch.setattr(app, "render_privacy_notice", MagicMock())
    monkeypatch.setattr(app, "render_forecast_disclaimer", MagicMock())
    monkeypatch.setattr(app, "render_error", display_error)
    failure = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "the local API is unavailable; start it and try again",
    )

    app.render_home(StubStatusApi(error=failure))

    display_error.assert_called_once_with(failure)
    ui.caption.assert_any_call(
        "Start the backend with `make api`, then refresh this page."
    )
    ui.columns.assert_not_called()


@pytest.mark.parametrize(
    ("page_id", "expects_disclaimer"),
    [
        (PageId.IMPORT, False),
        (PageId.FORECAST_AND_PLANNING, True),
    ],
)
def test_placeholder_is_truthful_and_forecast_warning_stays_visible(
    page_id: PageId,
    expects_disclaimer: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = MagicMock()
    empty = MagicMock()
    disclaimer = MagicMock()
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(app, "render_empty_state", empty)
    monkeypatch.setattr(app, "render_forecast_disclaimer", disclaimer)
    item = navigation_item(page_id)

    app.render_placeholder(item)

    ui.title.assert_called_once_with(f"{item.icon} {item.title}")
    empty.assert_called_once_with("This area is not implemented yet", item.summary)
    assert disclaimer.called is expects_disclaimer


def test_page_dispatch_opens_api_for_implemented_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = client
    client_factory = MagicMock(return_value=context)
    home = MagicMock()
    import_page = MagicMock(return_value=FrontendSessionState(account_id="account-1"))
    transaction_page = MagicMock(
        return_value=FrontendSessionState(account_id="account-2")
    )
    placeholder = MagicMock()
    monkeypatch.setattr(app, "ApiClient", client_factory)
    monkeypatch.setattr(app, "render_home", home)
    monkeypatch.setattr(app, "render_import_page", import_page)
    monkeypatch.setattr(app, "render_transaction_page", transaction_page)
    monkeypatch.setattr(app, "render_placeholder", placeholder)
    session = FrontendSessionState()

    home_result = app.render_application_page(
        navigation_item(PageId.HOME),
        base_url="http://127.0.0.1:8765",
        session=session,
    )
    import_result = app.render_application_page(
        navigation_item(PageId.IMPORT),
        base_url="http://127.0.0.1:8765",
        session=session,
    )
    transaction_result = app.render_application_page(
        navigation_item(PageId.TRANSACTIONS),
        base_url="http://127.0.0.1:8765",
        session=session,
    )

    placeholder_result = app.render_application_page(
        navigation_item(PageId.FORECAST_AND_PLANNING),
        base_url="http://127.0.0.1:8765",
        session=session,
    )

    assert client_factory.call_count == 3
    home.assert_called_once_with(client)
    import_page.assert_called_once_with(client, session)
    transaction_page.assert_called_once_with(client, session)
    placeholder.assert_called_once_with(navigation_item(PageId.FORECAST_AND_PLANNING))
    assert home_result == session
    assert import_result.account_id == "account-1"
    assert transaction_result.account_id == "account-2"
    assert placeholder_result == session


def test_application_main_restores_and_saves_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = MagicMock()
    ui.session_state = {}
    ui.sidebar.radio.return_value = "Import statements"
    rendered = MagicMock(
        side_effect=lambda item, base_url, session: session.model_copy(
            update={"account_id": "account-1"}
        )
    )
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(app, "load_settings", _settings)
    monkeypatch.setattr(app, "render_application_page", rendered)

    app.main()

    ui.set_page_config.assert_called_once()
    ui.sidebar.radio.assert_called_once_with(
        "Navigation",
        tuple(item.title for item in NAVIGATION_ITEMS),
        index=0,
    )
    assert ui.session_state[SESSION_KEY]["selected_page"] == "import"
    assert ui.session_state[SESSION_KEY]["account_id"] == "account-1"
    assert ui.session_state[SESSION_KEY]["privacy_notice_seen"] is True
    rendered.assert_called_once_with(
        navigation_item(PageId.IMPORT),
        base_url="http://127.0.0.1:8765",
        session=FrontendSessionState(
            selected_page=PageId.IMPORT,
            privacy_notice_seen=True,
        ),
    )


def test_packaged_cli_uses_loopback_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = MagicMock()
    monkeypatch.setattr(cli, "load_settings", _settings)
    monkeypatch.setattr("cashflow_ai.frontend.cli.streamlit_cli.main", launch)
    monkeypatch.setattr(sys, "argv", ["original"])

    cli.main()

    assert sys.argv[0:2] == ["streamlit", "run"]
    assert sys.argv[2].endswith("/cashflow_ai/frontend/app.py")
    assert sys.argv[3:] == [
        "--server.address",
        "127.0.0.1",
        "--server.port",
        "8766",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    launch.assert_called_once_with()

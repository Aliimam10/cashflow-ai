"""Shared Streamlit display states with controlled, privacy-safe wording."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from html import escape

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError


@contextmanager
def loading_state(message: str) -> Iterator[None]:
    """Display a consistent spinner around synchronous local work."""
    with st.spinner(message):
        yield


def render_error(error: ApiClientError) -> None:
    """Display a friendly error with its controlled identity kept secondary."""
    identity = error.code.value
    if error.problem_code is not None:
        identity = f"{identity}/{error.problem_code}"
    st.error(str(error))
    with st.expander("Technical details"):
        st.code(identity)


def render_page_header(eyebrow: str, title: str, description: str) -> None:
    """Render one consistent, readable page introduction."""
    st.markdown(
        (
            '<div class="cf-page-header">'
            f'<div class="cf-eyebrow">{escape(eyebrow)}</div>'
            f'<h1 class="cf-page-title">{escape(title)}</h1>'
            f'<p class="cf-page-description">{escape(description)}</p>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_feature_card(icon: str, title: str, description: str) -> None:
    """Render a compact product-capability card inside the current container."""
    st.markdown(
        (
            '<div class="cf-feature-card">'
            f'<div class="cf-feature-icon">{escape(icon)}</div>'
            f"<h3>{escape(title)}</h3>"
            f"<p>{escape(description)}</p>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_service_status(*, ready: bool) -> None:
    """Show local readiness without exposing infrastructure terminology."""
    tone = "" if ready else " is-warning"
    message = (
        "Everything is ready on this device"
        if ready
        else "The local service needs attention"
    )
    st.markdown(
        (
            f'<div class="cf-status{tone}">'
            '<span class="cf-status-dot"></span>'
            f"{message}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _render_notice(tone: str, title: str, guidance: str) -> None:
    st.markdown(
        (
            f'<div class="cf-notice {escape(tone)}">'
            f"<strong>{escape(title)}</strong> &nbsp;{escape(guidance)}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_empty_state(title: str, guidance: str) -> None:
    """Explain an intentionally empty or not-yet-configured view."""
    st.markdown(
        (
            '<div class="cf-empty-state">'
            f"<strong>{escape(title)}</strong><br>{escape(guidance)}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_privacy_notice() -> None:
    """Keep the local-first data boundary visible to the user."""
    _render_notice(
        "is-private",
        "Private by design.",
        "Your statements are processed on this device and are never sent to a "
        "bank or cloud service.",
    )


def render_forecast_disclaimer() -> None:
    """Keep uncertainty and advice limitations visible before forecast screens."""
    _render_notice(
        "is-caution",
        "Forecasts are estimates.",
        "Use them to explore possibilities, not as guaranteed financial advice.",
    )


__all__ = [
    "loading_state",
    "render_empty_state",
    "render_error",
    "render_feature_card",
    "render_forecast_disclaimer",
    "render_page_header",
    "render_privacy_notice",
    "render_service_status",
]

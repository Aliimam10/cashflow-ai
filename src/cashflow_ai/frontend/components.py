"""Shared Streamlit display states with controlled, privacy-safe wording."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError


@contextmanager
def loading_state(message: str) -> Iterator[None]:
    """Display a consistent spinner around synchronous local work."""
    with st.spinner(message):
        yield


def render_error(error: ApiClientError) -> None:
    """Display only the client's controlled message and error identity."""
    st.error(f"{error} · code: `{error.code.value}`")


def render_empty_state(title: str, guidance: str) -> None:
    """Explain an intentionally empty or not-yet-configured view."""
    st.info(f"**{title}**\n\n{guidance}")


def render_privacy_notice() -> None:
    """Keep the local-first data boundary visible to the user."""
    st.info(
        "**Privacy:** CashFlow AI is designed to process statements and financial "
        "data locally. Do not expose the unauthenticated API or UI beyond your "
        "computer."
    )


def render_forecast_disclaimer() -> None:
    """Keep uncertainty and advice limitations visible before forecast screens."""
    st.warning(
        "Forecasts and scenarios are estimates based on available history. They can "
        "be wrong and are not financial advice."
    )


__all__ = [
    "loading_state",
    "render_empty_state",
    "render_error",
    "render_forecast_disclaimer",
    "render_privacy_notice",
]

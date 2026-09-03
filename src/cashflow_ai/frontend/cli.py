"""Console entry point for the loopback-only Streamlit application."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli as streamlit_cli

from cashflow_ai.config import load_settings


def main() -> None:
    """Launch the packaged Streamlit script with privacy-safe local settings."""
    settings = load_settings()
    application = Path(__file__).with_name("app.py")
    sys.argv = [
        "streamlit",
        "run",
        str(application),
        "--server.address",
        settings.ui_host,
        "--server.port",
        str(settings.ui_port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    streamlit_cli.main()


__all__ = ["main"]

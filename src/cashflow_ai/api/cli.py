"""Development server entry point for the local API."""

from __future__ import annotations

import uvicorn

from cashflow_ai.api import build_container, create_app
from cashflow_ai.config import load_settings
from cashflow_ai.logging import configure_logging


def main() -> None:
    """Run the API on the configured loopback interface."""
    settings = load_settings()
    configure_logging(settings)
    uvicorn.run(
        create_app(build_container(settings)),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()

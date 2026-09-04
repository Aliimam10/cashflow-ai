"""Static safeguards for local containers and continuous integration."""

from pathlib import Path
from typing import Any, cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_CONTEXT_PATTERNS = {
    ".env",
    "uploads/",
    "data/",
    "models/",
    "artifacts/",
    "*.db",
    "*.sqlite",
    "*.joblib",
    "*.pkl",
}
PROHIBITED_SERVICES = {"postgres", "postgresql", "redis", "worker", "kafka"}


def _read_yaml(path: str) -> dict[str, Any]:
    payload = yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    return cast(dict[str, Any], payload)


def test_dockerfile_is_locked_local_and_unprivileged() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.12.0 AS uv" in dockerfile
    assert "FROM python:3.12.13-slim-bookworm AS runtime" in dockerfile
    assert "tesseract-ocr" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project --no-cache" in dockerfile
    assert "uv sync --locked --no-dev --no-editable --no-cache" in dockerfile
    assert "USER cashflow" in dockerfile
    assert "COPY . " not in dockerfile
    assert dockerfile.index("USER cashflow") > dockerfile.index("chown -R")


def test_docker_context_excludes_private_and_generated_data() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert patterns >= PRIVATE_CONTEXT_PATTERNS
    assert "!.env.example" in patterns


def test_compose_keeps_services_on_loopback_with_private_volumes() -> None:
    compose = _read_yaml("compose.yaml")
    services = cast(dict[str, dict[str, Any]], compose["services"])

    assert set(services) == {"api", "ui"}
    assert PROHIBITED_SERVICES.isdisjoint(services)
    assert services["api"]["ports"] == [
        "127.0.0.1:8000:8000",
        "127.0.0.1:8501:8501",
    ]
    assert services["ui"]["network_mode"] == "service:api"
    assert services["ui"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["api"]["volumes"] == [
        "cashflow_data:/app/data",
        "cashflow_models:/app/models",
    ]
    assert set(compose["volumes"]) == {"cashflow_data", "cashflow_models"}

    for service in services.values():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        environment = service["environment"]
        assert environment["CASHFLOW_API_HOST"] == "127.0.0.1"
        assert environment["CASHFLOW_UI_HOST"] == "127.0.0.1"
        assert environment["CASHFLOW_DATABASE_URL"] == (
            "sqlite:////app/data/cashflow.db"
        )


def test_ci_is_read_only_and_runs_every_required_gate() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "make format-check" in workflow
    assert "make lint" in workflow
    assert "make typecheck" in workflow
    assert "make test" in workflow
    assert "make check-import" in workflow
    assert "make db-upgrade" in workflow
    assert "make db-downgrade" in workflow
    assert "docker compose config --quiet" in workflow
    assert "docker build --tag cashflow-ai:ci ." in workflow
    assert "docker run --rm --entrypoint tesseract" in workflow
    assert "deploy" not in workflow.casefold()

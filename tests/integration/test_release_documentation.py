"""Release documentation and version-consistency safeguards."""

import re
import tomllib
from pathlib import Path

import cashflow_ai

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.0.0"
LOCAL_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|#)([^)#]+)(?:#[^)]+)?\)")


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_release_version_is_consistent() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    lock = tomllib.loads(_read("uv.lock"))
    locked_project = next(
        package for package in lock["package"] if package["name"] == "cashflow-ai"
    )

    assert cashflow_ai.__version__ == VERSION
    assert project["project"]["version"] == VERSION
    assert locked_project["version"] == VERSION
    assert f"## [{VERSION}]" in _read("CHANGELOG.md")
    assert f"v{VERSION} release candidate" in _read("docs/releases/v1.0.0.md")


def test_required_release_topics_are_documented_honestly() -> None:
    documentation = "\n".join(
        _read(path)
        for path in (
            "README.md",
            "docs/user_guide.md",
            "docs/evaluation.md",
            "docs/privacy.md",
            "docs/releases/v1.0.0.md",
        )
    ).casefold()

    for topic in (
        "current/checking",
        "savings",
        "statement gap",
        "transfer",
        "refund",
        "reimbursement",
        "categorisation",
        "forecast",
        "anomaly",
        "ocr",
        "synthetic",
        "not financial advice",
        "pdf approval",
    ):
        assert topic in documentation
    assert "pdf approval is not persisted" in documentation
    assert "not estimates" in documentation


def test_release_local_links_resolve() -> None:
    documents = (
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "CHANGELOG.md",
        PROJECT_ROOT / "docs/user_guide.md",
        PROJECT_ROOT / "docs/diagrams.md",
        PROJECT_ROOT / "docs/releases/v1.0.0.md",
    )

    for document in documents:
        for match in LOCAL_LINK.finditer(document.read_text(encoding="utf-8")):
            target = (document.parent / match.group(1)).resolve()
            assert target.exists(), f"broken local link in {document}: {match.group(1)}"

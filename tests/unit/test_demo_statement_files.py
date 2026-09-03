"""Tests for the fictional manual statement generator."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymupdf

from cashflow_ai.imports import extract_text_pdf


def test_demo_statement_generator_creates_digital_and_scanned_pdfs(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_demo_statements.py",
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    digital_path = tmp_path / "fictional_digital_statement.pdf"
    scanned_path = tmp_path / "fictional_scanned_statement.pdf"

    assert digital_path.name in completed.stdout
    assert scanned_path.name in completed.stdout
    assert digital_path.read_bytes().startswith(b"%PDF")
    assert scanned_path.read_bytes().startswith(b"%PDF")

    preview = extract_text_pdf(
        digital_path.read_bytes(),
        filename=digital_path.name,
        mime_type="application/pdf",
        account_id="synthetic-account",
    )
    assert len(preview.candidates) == 2
    assert preview.statement_coverage is not None
    assert preview.statement_coverage.statement_start_date.isoformat() == "2026-08-01"

    with pymupdf.open(  # type: ignore[no-untyped-call]
        stream=scanned_path.read_bytes(), filetype="pdf"
    ) as document:
        assert not document[0].get_text().strip()

"""Generate fictional PDF statements for manual import-interface checks."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pymupdf
from PIL import Image, ImageDraw, ImageFont

STATEMENT_LINES = (
    "Fictional Example Bank",
    "Statement period: 01 August 2026 to 31 August 2026",
    "Opening balance: GBP 1000.00",
    "Date | Description | Amount | Balance",
    "2026-08-01 | SYNTHETIC RENT | -400.00 | 600.00",
    "2026-08-15 | SYNTHETIC PAY | 1000.00 | 1600.00",
    "Closing balance: GBP 1600.00",
)


def _finish_pdf(document: Any) -> bytes:
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _digital_pdf() -> bytes:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    for index, line in enumerate(STATEMENT_LINES):
        page.insert_text((45, 45 + index * 24), line, fontsize=10)
    return _finish_pdf(document)


def _scanned_pdf() -> bytes:
    image = Image.new("RGB", (1800, 1200), "white")
    drawing = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    for index, line in enumerate(STATEMENT_LINES):
        drawing.text((80, 80 + index * 100), line, fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()

    document = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=900, height=600)
    page.insert_image(  # type: ignore[no-untyped-call]
        page.rect,
        stream=output.getvalue(),
    )
    return _finish_pdf(document)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/demo/generated/statements"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Write both fictional statement variants and report their paths."""
    output_directory = build_parser().parse_args(argv).output_dir
    output_directory.mkdir(parents=True, exist_ok=True)
    statements = {
        "fictional_digital_statement.pdf": _digital_pdf(),
        "fictional_scanned_statement.pdf": _scanned_pdf(),
    }
    for filename, content in statements.items():
        path = output_directory / filename
        path.write_bytes(content)
        print(f"generated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

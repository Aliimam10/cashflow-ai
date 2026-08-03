"""Tests for the synthetic-data command-line interface."""

import sys
from pathlib import Path

import pytest

from cashflow_ai.demo_data.cli import main


def test_cli_generates_all_profiles_and_layouts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "--profile",
            "all",
            "--seed",
            "7",
            "--years",
            "1",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert len(list(tmp_path.rglob("*.csv"))) == 9
    output = capsys.readouterr().out
    assert "student transactions" in output
    assert "salaried_worker transactions" in output
    assert "irregular_income_worker transactions" in output


def test_cli_supports_one_profile_and_layout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "--profile",
            "student",
            "--start-date",
            "2023-06-15",
            "--years",
            "1",
            "--layout",
            "canonical",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert len(list(tmp_path.rglob("*.csv"))) == 1
    assert "across 1 layout(s)" in capsys.readouterr().out


def test_cli_uses_process_arguments_when_none_are_passed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_demo_data.py",
            "--profile",
            "student",
            "--years",
            "1",
            "--layout",
            "signed_amount",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert len(list(tmp_path.rglob("*.csv"))) == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--start-date", "31-01-2024"], "expected an ISO date"),
        (["--years", "0"], "years must be 1, 2, or 3"),
    ],
)
def test_cli_rejects_invalid_arguments(
    arguments: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(arguments)

    assert message in capsys.readouterr().err

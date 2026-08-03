"""Command-line interface for synthetic demo-data generation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from cashflow_ai.demo_data.export import CsvLayout, export_dataset
from cashflow_ai.demo_data.generator import generate_dataset
from cashflow_ai.demo_data.models import SyntheticProfile


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = "expected an ISO date such as 2024-01-01"
        raise argparse.ArgumentTypeError(msg) from exc


def _years_argument(value: str) -> int:
    years = int(value)
    if years not in {1, 2, 3}:
        msg = "years must be 1, 2, or 3"
        raise argparse.ArgumentTypeError(msg)
    return years


def build_parser() -> argparse.ArgumentParser:
    """Create the demo-data CLI parser."""
    parser = argparse.ArgumentParser(
        description="Generate reproducible fictional CashFlow AI transactions.",
    )
    parser.add_argument(
        "--profile",
        choices=("all", *(profile.value for profile in SyntheticProfile)),
        default="all",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", type=_date_argument, default=date(2024, 1, 1))
    parser.add_argument("--years", type=_years_argument, default=2)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/demo/generated"),
    )
    parser.add_argument(
        "--layout",
        action="append",
        choices=tuple(layout.value for layout in CsvLayout),
        dest="layouts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate requested profile files and return a process exit code."""
    arguments = build_parser().parse_args(argv)
    profiles = (
        tuple(SyntheticProfile)
        if arguments.profile == "all"
        else (SyntheticProfile(arguments.profile),)
    )
    layouts = (
        tuple(CsvLayout(layout) for layout in arguments.layouts)
        if arguments.layouts
        else tuple(CsvLayout)
    )

    for profile in profiles:
        dataset = generate_dataset(
            profile,
            seed=arguments.seed,
            start_date=arguments.start_date,
            years=arguments.years,
        )
        profile_directory = arguments.output_dir / profile.value
        paths = export_dataset(dataset, profile_directory, layouts=layouts)
        print(
            f"generated {len(dataset.transactions)} {profile.value} transactions "
            f"across {len(paths)} layout(s) in {profile_directory}"
        )
    return 0

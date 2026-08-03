"""Tests for synthetic CSV layouts."""

import csv
from pathlib import Path

import pytest

from cashflow_ai.demo_data import (
    CsvLayout,
    SyntheticProfile,
    export_dataset,
    generate_dataset,
)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def test_all_csv_layouts_are_exported(tmp_path: Path) -> None:
    dataset = generate_dataset(SyntheticProfile.STUDENT, seed=22, years=1)

    paths = export_dataset(dataset, tmp_path)

    assert set(paths) == set(CsvLayout)
    assert all(path.exists() for path in paths.values())

    canonical_rows = read_rows(paths[CsvLayout.CANONICAL])
    assert len(canonical_rows) == len(dataset.transactions)
    assert set(canonical_rows[0]) == {
        "transaction_date",
        "posting_date",
        "description",
        "merchant",
        "amount",
        "balance",
        "currency",
        "account",
        "transaction_type",
        "category",
        "is_recurring",
        "recurring_series",
        "anomaly_type",
        "duplicate_of",
        "external_id",
    }
    assert any(row["anomaly_type"] == "exact_duplicate" for row in canonical_rows)
    assert any(row["recurring_series"] == "" for row in canonical_rows)

    debit_credit_rows = read_rows(paths[CsvLayout.DEBIT_CREDIT])
    assert any(row["Debit"] and not row["Credit"] for row in debit_credit_rows)
    assert any(row["Credit"] and not row["Debit"] for row in debit_credit_rows)
    assert set(debit_credit_rows[0]) == {
        "Date",
        "Details",
        "Debit",
        "Credit",
        "Balance",
        "Currency",
        "Account",
        "Reference",
    }

    signed_rows = read_rows(paths[CsvLayout.SIGNED_AMOUNT])
    assert set(signed_rows[0]) == {
        "Posted Date",
        "Narrative",
        "Amount",
        "Running Balance",
        "Type",
        "Reference",
    }


def test_selected_layout_can_be_exported_alone(tmp_path: Path) -> None:
    dataset = generate_dataset(SyntheticProfile.SALARIED_WORKER, years=1)

    paths = export_dataset(
        dataset,
        tmp_path / "nested",
        layouts=[CsvLayout.SIGNED_AMOUNT],
    )

    assert list(paths) == [CsvLayout.SIGNED_AMOUNT]
    assert paths[CsvLayout.SIGNED_AMOUNT].name.endswith("signed_amount.csv")


def test_export_requires_at_least_one_layout(tmp_path: Path) -> None:
    dataset = generate_dataset(SyntheticProfile.STUDENT, years=1)

    with pytest.raises(ValueError, match="at least one"):
        export_dataset(dataset, tmp_path, layouts=[])

"""CSV layouts for generated synthetic datasets."""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final

from cashflow_ai.demo_data.models import SyntheticDataset, SyntheticTransaction


class CsvLayout(StrEnum):
    """Available synthetic CSV layouts."""

    CANONICAL = "canonical"
    DEBIT_CREDIT = "debit_credit"
    SIGNED_AMOUNT = "signed_amount"


def _money_text(transaction: SyntheticTransaction) -> str:
    return format(transaction.amount, ".2f")


def _balance_text(transaction: SyntheticTransaction) -> str:
    return format(transaction.balance_after, ".2f")


def _canonical_row(transaction: SyntheticTransaction) -> dict[str, object]:
    return {
        "transaction_date": transaction.transaction_date.isoformat(),
        "posting_date": transaction.posting_date.isoformat(),
        "description": transaction.description,
        "merchant": transaction.merchant,
        "amount": _money_text(transaction),
        "balance": _balance_text(transaction),
        "currency": transaction.currency,
        "account": transaction.account,
        "transaction_type": transaction.transaction_type,
        "category": transaction.category,
        "is_recurring": transaction.is_recurring,
        "recurring_series": transaction.recurring_series or "",
        "anomaly_type": transaction.anomaly_type or "",
        "duplicate_of": transaction.duplicate_of or "",
        "external_id": transaction.external_id,
    }


def _debit_credit_row(transaction: SyntheticTransaction) -> dict[str, object]:
    return {
        "Date": transaction.transaction_date.strftime("%d/%m/%Y"),
        "Details": transaction.description,
        "Debit": format(abs(transaction.amount), ".2f")
        if transaction.amount < 0
        else "",
        "Credit": _money_text(transaction) if transaction.amount > 0 else "",
        "Balance": _balance_text(transaction),
        "Currency": transaction.currency,
        "Account": transaction.account,
        "Reference": transaction.external_id,
    }


def _signed_amount_row(transaction: SyntheticTransaction) -> dict[str, object]:
    return {
        "Posted Date": transaction.posting_date.strftime("%Y-%m-%d"),
        "Narrative": transaction.description,
        "Amount": _money_text(transaction),
        "Running Balance": _balance_text(transaction),
        "Type": transaction.transaction_type,
        "Reference": transaction.external_id,
    }


ROW_BUILDERS: Final[
    Mapping[CsvLayout, Callable[[SyntheticTransaction], dict[str, object]]]
] = {
    CsvLayout.CANONICAL: _canonical_row,
    CsvLayout.DEBIT_CREDIT: _debit_credit_row,
    CsvLayout.SIGNED_AMOUNT: _signed_amount_row,
}


def export_dataset(
    dataset: SyntheticDataset,
    output_directory: Path,
    *,
    layouts: Iterable[CsvLayout] = tuple(CsvLayout),
) -> dict[CsvLayout, Path]:
    """Write selected CSV layouts and return their paths."""
    selected_layouts = tuple(layouts)
    if not selected_layouts:
        msg = "at least one CSV layout must be selected"
        raise ValueError(msg)

    output_directory.mkdir(parents=True, exist_ok=True)
    paths: dict[CsvLayout, Path] = {}

    for layout in selected_layouts:
        path = output_directory / f"{dataset.profile.value}_{layout.value}.csv"
        row_builder = ROW_BUILDERS[layout]
        rows = [row_builder(transaction) for transaction in dataset.transactions]
        with path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        paths[layout] = path

    return paths

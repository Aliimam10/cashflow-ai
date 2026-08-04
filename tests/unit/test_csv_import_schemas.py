"""Tests for CSV preview and user-selected mapping contracts."""

from datetime import date

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    CoverageStatus,
    CsvColumnMapping,
    CsvImportPlan,
    ImportContext,
    StatementCoverage,
)


def statement_context(account_id: str = "account-1") -> ImportContext:
    return ImportContext(
        account_id=account_id,
        coverage=StatementCoverage(
            statement_start_date=date(2026, 7, 1),
            statement_end_date=date(2026, 7, 31),
            status=CoverageStatus.COMPLETE,
        ),
    )


def test_signed_amount_mapping_and_import_plan_are_valid() -> None:
    mapping = CsvColumnMapping(
        transaction_date_column="Date",
        description_column="Narrative",
        signed_amount_column="Amount",
        posting_date_column="Posting Date",
        running_balance_column="Balance",
        external_id_column="Transaction ID",
        transaction_type_column="Type",
    )
    plan = CsvImportPlan(
        account_id="account-1",
        statement_context=statement_context(),
        mapping=mapping,
    )

    assert plan.mapping.signed_amount_column == "Amount"
    assert plan.mapping.source_columns == (
        "Date",
        "Narrative",
        "Amount",
        "Posting Date",
        "Balance",
        "Transaction ID",
        "Type",
    )


def test_separate_debit_and_credit_mapping_is_valid() -> None:
    mapping = CsvColumnMapping(
        transaction_date_column="Date",
        description_column="Details",
        debit_amount_column="Money Out",
        credit_amount_column="Money In",
    )

    assert mapping.debit_amount_column == "Money Out"
    assert mapping.credit_amount_column == "Money In"


@pytest.mark.parametrize(
    "amount_columns",
    [
        {},
        {"debit_amount_column": "Debit"},
        {"credit_amount_column": "Credit"},
        {
            "signed_amount_column": "Amount",
            "debit_amount_column": "Debit",
            "credit_amount_column": "Credit",
        },
    ],
)
def test_mapping_requires_exactly_one_complete_amount_layout(
    amount_columns: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="either one signed amount"):
        CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            **amount_columns,
        )


def test_one_source_column_cannot_have_two_meanings() -> None:
    with pytest.raises(ValidationError, match="only one import field"):
        CsvColumnMapping(
            transaction_date_column="Date",
            description_column="DESCRIPTION",
            signed_amount_column="Amount",
            transaction_type_column="description",
        )


def test_import_plan_rejects_context_for_a_different_account() -> None:
    mapping = CsvColumnMapping(
        transaction_date_column="Date",
        description_column="Description",
        signed_amount_column="Amount",
    )

    with pytest.raises(ValidationError, match="must match"):
        CsvImportPlan(
            account_id="account-1",
            statement_context=statement_context("account-2"),
            mapping=mapping,
        )

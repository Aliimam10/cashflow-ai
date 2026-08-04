"""CSV preview, column-mapping, and import-selection contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.statements import ImportContext
from cashflow_ai.schemas.transactions import Currency, Identifier

ColumnName = Annotated[str, Field(min_length=1, max_length=255)]


class CsvEncoding(StrEnum):
    """Text encodings detected by the Version 1 CSV adapter."""

    UTF_8 = "utf-8"
    UTF_8_SIG = "utf-8-sig"
    UTF_16 = "utf-16"
    WINDOWS_1252 = "windows-1252"


class _CsvContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class CsvPreviewRow(_CsvContract):
    """One source row retained in a limited, non-persistent preview."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    source_row_number: PositiveInt
    values: tuple[str, ...]


class CsvColumnSuggestions(_CsvContract):
    """Source columns that may satisfy each canonical import field."""

    transaction_date: tuple[ColumnName, ...] = ()
    posting_date: tuple[ColumnName, ...] = ()
    description: tuple[ColumnName, ...] = ()
    signed_amount: tuple[ColumnName, ...] = ()
    debit_amount: tuple[ColumnName, ...] = ()
    credit_amount: tuple[ColumnName, ...] = ()
    running_balance: tuple[ColumnName, ...] = ()
    currency: tuple[ColumnName, ...] = ()
    external_id: tuple[ColumnName, ...] = ()
    transaction_type: tuple[ColumnName, ...] = ()


class CsvPreview(_CsvContract):
    """Safe structural preview of an uploaded CSV file."""

    source_filename: str = Field(min_length=1, max_length=255)
    byte_size: PositiveInt
    encoding: CsvEncoding
    delimiter: str = Field(min_length=1, max_length=1)
    columns: tuple[ColumnName, ...] = Field(min_length=1)
    rows: tuple[CsvPreviewRow, ...] = Field(min_length=1)
    total_data_rows: PositiveInt
    truncated: bool
    suggestions: CsvColumnSuggestions


class CsvColumnMapping(_CsvContract):
    """User-selected mapping from CSV headings to import fields."""

    transaction_date_column: ColumnName
    description_column: ColumnName
    signed_amount_column: ColumnName | None = None
    debit_amount_column: ColumnName | None = None
    credit_amount_column: ColumnName | None = None
    posting_date_column: ColumnName | None = None
    running_balance_column: ColumnName | None = None
    currency_column: ColumnName | None = None
    external_id_column: ColumnName | None = None
    transaction_type_column: ColumnName | None = None

    @model_validator(mode="after")
    def validate_amount_layout_and_unique_columns(self) -> CsvColumnMapping:
        """Require one amount layout and one meaning per source column."""
        has_signed = self.signed_amount_column is not None
        has_debit = self.debit_amount_column is not None
        has_credit = self.credit_amount_column is not None
        if has_signed == (has_debit and has_credit):
            msg = "map either one signed amount column or both debit and credit columns"
            raise ValueError(msg)

        mapped_columns = [
            value
            for value in (
                self.transaction_date_column,
                self.description_column,
                self.signed_amount_column,
                self.debit_amount_column,
                self.credit_amount_column,
                self.posting_date_column,
                self.running_balance_column,
                self.currency_column,
                self.external_id_column,
                self.transaction_type_column,
            )
            if value is not None
        ]
        normalised = [value.casefold() for value in mapped_columns]
        if len(normalised) != len(set(normalised)):
            msg = "each CSV column can be mapped to only one import field"
            raise ValueError(msg)
        return self

    @property
    def source_columns(self) -> tuple[str, ...]:
        """Return every selected source column in canonical-field order."""
        return tuple(
            value
            for value in (
                self.transaction_date_column,
                self.description_column,
                self.signed_amount_column,
                self.debit_amount_column,
                self.credit_amount_column,
                self.posting_date_column,
                self.running_balance_column,
                self.currency_column,
                self.external_id_column,
                self.transaction_type_column,
            )
            if value is not None
        )


class CsvImportPlan(_CsvContract):
    """Account, statement context, and column choices for a CSV import."""

    account_id: Identifier
    account_currency: Currency = Currency.GBP
    statement_context: ImportContext
    mapping: CsvColumnMapping

    @model_validator(mode="after")
    def validate_context_account(self) -> CsvImportPlan:
        """Prevent a statement from being imported into a different account."""
        if self.account_id != self.statement_context.account_id:
            msg = "selected account must match the statement-context account"
            raise ValueError(msg)
        return self

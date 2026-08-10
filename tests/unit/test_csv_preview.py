"""Tests for safe CSV decoding, preview, suggestions, and validation."""

from datetime import date

import pytest
from pydantic import ValidationError

from cashflow_ai.imports import (
    CsvImportError,
    CsvImportErrorCode,
    calculate_file_hash,
    parse_csv_document,
    preview_csv,
    validate_csv_import_plan,
)
from cashflow_ai.schemas import (
    CoverageStatus,
    CsvColumnMapping,
    CsvEncoding,
    CsvImportPlan,
    ImportContext,
    StatementCoverage,
)


def import_plan(**mapping_updates: str) -> CsvImportPlan:
    mapping_values = {
        "transaction_date_column": "Date",
        "description_column": "Description",
        "signed_amount_column": "Amount",
        **mapping_updates,
    }
    return CsvImportPlan(
        account_id="account-1",
        statement_context=ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 7, 1),
                statement_end_date=date(2026, 7, 31),
                status=CoverageStatus.UNKNOWN,
            ),
        ),
        mapping=CsvColumnMapping.model_validate(mapping_values),
    )


def assert_error(
    content: bytes,
    filename: str,
    code: CsvImportErrorCode,
    **limits: int,
) -> None:
    with pytest.raises(CsvImportError) as error:
        preview_csv(content, filename, **limits)

    assert error.value.code is code


def test_utf8_signed_amount_preview_is_limited_but_counts_every_row() -> None:
    content = (
        "Transaction Date,Narrative,Amount,Running Balance,Currency,"
        "Transaction ID,Type\n"
        "2026-07-01,Caf\N{LATIN SMALL LETTER E WITH ACUTE},-4.50,995.50,"
        "GBP,txn-1,Card\n"
        "2026-07-02,Salary,1200.00,2195.50,GBP,txn-2,Transfer\n"
    ).encode()

    preview = preview_csv(content, "../../July statement.csv", preview_rows=1)

    assert preview.source_filename == "July statement.csv"
    assert preview.file_hash == calculate_file_hash(content)
    assert preview.encoding is CsvEncoding.UTF_8
    assert preview.delimiter == ","
    assert preview.total_data_rows == 2
    assert preview.truncated is True
    assert preview.rows[0].source_row_number == 2
    assert preview.rows[0].values[1] == "Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    assert preview.suggestions.transaction_date == ("Transaction Date",)
    assert preview.suggestions.description == ("Narrative",)
    assert preview.suggestions.signed_amount == ("Amount",)
    assert preview.suggestions.running_balance == ("Running Balance",)
    assert preview.suggestions.currency == ("Currency",)
    assert preview.suggestions.external_id == ("Transaction ID",)
    assert preview.suggestions.transaction_type == ("Type",)


def test_windows_1252_semicolon_file_suggests_separate_amount_columns() -> None:
    content = (
        "Date;Posting Date;Details;Paid Out;Paid In;Balance\n"
        "01/07/2026;02/07/2026;Caf\N{LATIN SMALL LETTER E WITH ACUTE};4,50;;995,50\n"
    ).encode("cp1252")

    preview = preview_csv(content, r"C:\fakepath\statement.CSV")

    assert preview.source_filename == "statement.CSV"
    assert preview.encoding is CsvEncoding.WINDOWS_1252
    assert preview.delimiter == ";"
    assert preview.truncated is False
    assert preview.suggestions.transaction_date == ("Date",)
    assert preview.suggestions.posting_date == ("Posting Date",)
    assert preview.suggestions.debit_amount == ("Paid Out",)
    assert preview.suggestions.credit_amount == ("Paid In",)


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("utf-8-sig", CsvEncoding.UTF_8_SIG),
        ("utf-16", CsvEncoding.UTF_16),
    ],
)
def test_bom_encodings_are_detected(encoding: str, expected: CsvEncoding) -> None:
    content = "Date,Description,Amount\n2026-07-01,Example,-1.00\n".encode(encoding)

    preview = preview_csv(content, "statement.csv")

    assert preview.encoding is expected


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"   \r\n", "empty"),
        (b"Date,Description,Amount\n", "no data rows"),
    ],
)
def test_empty_files_are_rejected(content: bytes, message: str) -> None:
    with pytest.raises(CsvImportError, match=message) as error:
        preview_csv(content, "statement.csv")

    assert error.value.code is CsvImportErrorCode.EMPTY_FILE


@pytest.mark.parametrize(
    ("max_bytes", "preview_rows"),
    [(0, 1), (1, 0)],
)
def test_limits_must_be_positive(max_bytes: int, preview_rows: int) -> None:
    assert_error(
        b"Date,Amount\n2026-01-01,1\n",
        "statement.csv",
        CsvImportErrorCode.INVALID_LIMIT,
        max_bytes=max_bytes,
        preview_rows=preview_rows,
    )


def test_full_document_parser_keeps_all_rows_and_validates_its_limit() -> None:
    content = (
        b"Date,Description,Amount\n2026-01-01,First,-1.00\n2026-01-02,Second,-2.00\n"
    )

    document = parse_csv_document(content, "statement.csv")

    assert len(document.rows) == 2
    assert document.rows[-1].source_row_number == 3
    assert document.file_hash == calculate_file_hash(content)
    with pytest.raises(CsvImportError) as error:
        parse_csv_document(content, "statement.csv", max_bytes=0)
    assert error.value.code is CsvImportErrorCode.INVALID_LIMIT


@pytest.mark.parametrize("filename", ["", ".", "x" * 252 + ".csv"])
def test_invalid_filenames_are_rejected(filename: str) -> None:
    assert_error(
        b"Date,Amount\n2026-01-01,1\n",
        filename,
        CsvImportErrorCode.INVALID_FILENAME,
    )


def test_non_csv_filename_is_rejected() -> None:
    assert_error(
        b"Date,Amount\n2026-01-01,1\n",
        "statement.pdf",
        CsvImportErrorCode.UNSUPPORTED_FILE_TYPE,
    )


def test_file_size_limit_is_enforced_before_decoding() -> None:
    assert_error(
        b"Date,Amount\n2026-01-01,1\n",
        "statement.csv",
        CsvImportErrorCode.FILE_TOO_LARGE,
        max_bytes=5,
    )


def test_unsupported_encoding_is_rejected() -> None:
    assert_error(
        b"Date,Description,Amount\n2026-01-01,\x81,1\n",
        "statement.csv",
        CsvImportErrorCode.UNSUPPORTED_ENCODING,
    )


def test_binary_control_characters_are_rejected() -> None:
    assert_error(
        b"Date,Description,Amount\n2026-01-01,bad\x00value,1\n",
        "statement.csv",
        CsvImportErrorCode.BINARY_CONTENT,
    )


def test_unknown_delimiter_is_rejected() -> None:
    assert_error(
        b"this has no tabular structure",
        "statement.csv",
        CsvImportErrorCode.MALFORMED_CSV,
    )


@pytest.mark.parametrize(
    "header",
    [
        ",Description,Amount",
        "Date,Description,description",
        f"{'x' * 256},Description,Amount",
        ",".join(f"column-{number}" for number in range(101)),
    ],
)
def test_invalid_headers_are_rejected(header: str) -> None:
    row = ",".join("value" for _ in header.split(","))

    assert_error(
        f"{header}\n{row}\n".encode(),
        "statement.csv",
        CsvImportErrorCode.INVALID_HEADER,
    )


def test_inconsistent_column_count_is_rejected_even_after_preview_limit() -> None:
    content = (
        b"Date,Description,Amount\n2026-01-01,First,-1.00\n2026-01-02,Missing amount\n"
    )

    assert_error(
        content,
        "statement.csv",
        CsvImportErrorCode.MALFORMED_CSV,
        preview_rows=1,
    )


def test_oversized_cell_is_rejected() -> None:
    oversized = "x" * 100_001
    content = f"Date,Description,Amount\n2026-01-01,{oversized},-1.00\n".encode()

    assert_error(content, "statement.csv", CsvImportErrorCode.MALFORMED_CSV)


def test_malformed_quoting_is_rejected() -> None:
    assert_error(
        b'Date,Description,Amount\n2026-01-01,"unfinished,-1.00\n',
        "statement.csv",
        CsvImportErrorCode.MALFORMED_CSV,
    )


def test_mapping_is_checked_against_preview_columns_case_insensitively() -> None:
    preview = preview_csv(
        b"Date,Description,Amount\n2026-01-01,Example,-1.00\n",
        "statement.csv",
    )
    plan = import_plan(
        transaction_date_column="date",
        description_column="DESCRIPTION",
        signed_amount_column="amount",
    )

    assert validate_csv_import_plan(preview, plan) is plan


def test_mapping_rejects_columns_absent_from_preview() -> None:
    preview = preview_csv(
        b"Date,Description,Amount\n2026-01-01,Example,-1.00\n",
        "statement.csv",
    )
    plan = import_plan(running_balance_column="Balance")

    with pytest.raises(CsvImportError, match="Balance") as error:
        validate_csv_import_plan(preview, plan)

    assert error.value.code is CsvImportErrorCode.MISSING_MAPPED_COLUMN


def test_mapping_schema_still_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CsvColumnMapping.model_validate(
            {
                "transaction_date_column": "Date",
                "description_column": "Description",
                "signed_amount_column": "Amount",
                "category_column": "Category",
            }
        )

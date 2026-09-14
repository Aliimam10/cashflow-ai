"""Tests for safe CSV decoding, preview, suggestions, and validation."""

import csv
from datetime import date
from io import StringIO

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


def _padded_row(*values: str) -> list[str]:
    return [*values, *("" for _ in range(13 - len(values)))]


def _consolidated_csv(
    *,
    currency_symbol: str = "£",
    duplicate_gbp_table: bool = False,
    malformed_row: bool = False,
    unexpected_trailing_value: bool = False,
    missing_total: bool = False,
    total_mismatch: bool = False,
    balance_mismatch: bool = False,
    reverse_dates: bool = False,
    missing_category: bool = False,
    invalid_tax: bool = False,
    invalid_calendar_date: bool = False,
    include_non_gbp_table: bool = False,
    include_empty_gbp_table: bool = False,
    include_blank_gbp_section: bool = False,
    include_changed_table: bool = False,
    non_gbp_row: bool = True,
    non_gbp_total: bool = True,
    non_gbp_blank_termination: bool = False,
    footer_issue: str | None = None,
) -> bytes:
    """Build a fictional multi-section export without bank or user data."""
    header = _padded_row(
        "Date",
        "Description",
        "Category",
        "Money in/out",
        "Balance",
        "Tax withheld",
        "Other taxes",
        "Fees",
    )
    first = _padded_row(
        "Apr 1, 2026",
        "SYNTHETIC PAY",
        "Income",
        f"{currency_symbol}100.00",
        f"{currency_symbol}600.00",
        f"{currency_symbol}0.00",
        f"{currency_symbol}0.00",
        f"{currency_symbol}0.00",
    )
    second = _padded_row(
        "Apr 2, 2026",
        "SYNTHETIC FOOD",
        "Food",
        f"-{currency_symbol}20.00",
        f"{currency_symbol}580.00",
        f"{currency_symbol}0.00",
        f"{currency_symbol}0.00",
        f"{currency_symbol}0.00",
    )
    if malformed_row:
        second[0] = "not-a-date"
    if unexpected_trailing_value:
        second[-1] = "unexpected"
    if balance_mismatch:
        second[4] = f"{currency_symbol}579.00"
    if reverse_dates:
        second[0] = "Mar 31, 2026"
    if missing_category:
        second[2] = ""
    if invalid_tax:
        second[5] = "not-money"
    if invalid_calendar_date:
        second[0] = "Apr 31, 2026"
    total = f"{currency_symbol}{'81.00' if total_mismatch else '80.00'}"
    footer = _padded_row("Total", "", "", total)
    if footer_issue == "short":
        footer = ["Total"]
    elif footer_issue == "trailing":
        footer[-1] = "unexpected"
    elif footer_issue == "invalid_money":
        footer[3] = "not-money"
    table = [
        header,
        first,
        second,
        *(() if missing_total else (footer,)),
        _padded_row(),
    ]
    rows = [
        _padded_row("Fictional consolidated statement"),
        _padded_row(),
        *table,
        _padded_row("Other currency summary"),
    ]
    if include_non_gbp_table:
        rows.append(
            _padded_row(
                "Date",
                "Description",
                "Category",
                "Money in/out",
                "Money in/out",
                "Balance",
                "Balance",
                "Tax withheld",
                "Tax withheld",
                "Other taxes",
                "Other taxes",
                "Fees",
                "Fees",
            )
        )
        if non_gbp_row:
            rows.append(
                _padded_row(
                    "Apr 3, 2026",
                    "SYNTHETIC FOREIGN PURCHASE",
                    "Card",
                    "-€10.00",
                    "-£8.50",
                    "€90.00",
                    "£76.50",
                    "€0.00",
                    "£0.00",
                    "€0.00",
                    "£0.00",
                    "€0.00",
                    "£0.00",
                ),
            )
        if non_gbp_total:
            rows.append(_padded_row("Total", "", "", "-€10.00"))
        if non_gbp_blank_termination:
            rows.append(_padded_row())
    if include_empty_gbp_table:
        rows.extend(
            (
                header,
                _padded_row(
                    "Total",
                    "",
                    "",
                    f"{currency_symbol}0.00",
                    "",
                    f"{currency_symbol}0.00",
                    f"{currency_symbol}0.00",
                    f"{currency_symbol}0.00",
                ),
                _padded_row(),
            )
        )
    if include_blank_gbp_section:
        rows.extend((header, _padded_row()))
    if include_changed_table:
        rows.append(
            _padded_row(
                "Date",
                "Description",
                "Category",
                "Changed field",
                "Money in/out",
            )
        )
    if duplicate_gbp_table:
        rows.extend(table)
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


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
    assert preview.suggested_date_column == "Transaction Date"
    assert preview.suggested_statement_period is not None
    assert preview.suggested_statement_period.start_date == date(2026, 7, 1)
    assert preview.suggested_statement_period.end_date == date(2026, 7, 2)


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
    assert preview.suggested_date_column == "Date"
    assert preview.suggested_statement_period is not None
    assert preview.suggested_statement_period.start_date == date(2026, 7, 1)
    assert preview.suggested_statement_period.end_date == date(2026, 7, 1)


def test_preview_date_bounds_use_the_full_file_and_ignore_invalid_rows() -> None:
    content = (
        b"Date,Description,Amount\n"
        b"31/08/2026,Latest,-1.00\n"
        b"not-a-date,Rejected,-2.00\n"
        b"2025-09-01,Earliest,-3.00\n"
    )

    preview = preview_csv(content, "statement.csv", preview_rows=1)

    assert preview.truncated is True
    assert preview.suggested_statement_period is not None
    assert preview.suggested_statement_period.start_date == date(2025, 9, 1)
    assert preview.suggested_statement_period.end_date == date(2026, 8, 31)


def test_snake_case_bank_headers_are_recognised_for_date_detection() -> None:
    content = (
        b"transaction_date,description,amount,running_balance\n"
        b"2025-09-01,Synthetic income,1000.00,1000.00\n"
        b"2026-08-31,Synthetic rent,-100.00,900.00\n"
    )

    preview = preview_csv(content, "synthetic_history.csv")

    assert preview.suggestions.transaction_date == ("transaction_date",)
    assert preview.suggestions.running_balance == ("running_balance",)
    assert preview.suggested_date_column == "transaction_date"
    assert preview.suggested_statement_period is not None
    assert preview.suggested_statement_period.start_date == date(2025, 9, 1)
    assert preview.suggested_statement_period.end_date == date(2026, 8, 31)


def test_consolidated_export_selects_one_strict_gbp_transaction_table() -> None:
    content = _consolidated_csv(include_non_gbp_table=True)

    preview = preview_csv(content, "fictional-consolidated.csv")
    document = parse_csv_document(content, "fictional-consolidated.csv")

    assert preview.columns == (
        "Date",
        "Description",
        "Category",
        "Money in/out",
        "Balance",
        "Tax withheld",
        "Other taxes",
        "Fees",
    )
    assert preview.total_data_rows == 2
    assert [row.source_row_number for row in document.rows] == [4, 5]
    assert document.parser_name == "revolut_consolidated_csv"
    assert document.parser_version == "1.0.0"
    assert document.layout_version == "consolidated_v2_gbp_1"
    assert document.warning_codes == ("non_gbp_transaction_sections_excluded",)
    assert document.excluded_transaction_rows == 1
    gbp_only = parse_csv_document(
        _consolidated_csv(),
        "fictional-gbp-only-consolidated.csv",
    )
    assert gbp_only.warning_codes == ()
    assert gbp_only.excluded_transaction_rows == 0
    assert preview.suggestions.transaction_date == ("Date",)
    assert preview.suggestions.description == ("Description",)
    assert preview.suggestions.signed_amount == ("Money in/out",)
    assert preview.suggestions.running_balance == ("Balance",)
    assert preview.suggested_statement_period is not None
    assert preview.suggested_statement_period.start_date == date(2026, 4, 1)
    assert preview.suggested_statement_period.end_date == date(2026, 4, 2)


def test_consolidated_export_ignores_an_empty_repeated_gbp_section() -> None:
    document = parse_csv_document(
        _consolidated_csv(
            include_non_gbp_table=True,
            include_empty_gbp_table=True,
        ),
        "fictional-consolidated.csv",
    )

    assert document.parser_name == "revolut_consolidated_csv"
    assert len(document.rows) == 2
    assert document.warning_codes == (
        "non_gbp_transaction_sections_excluded",
        "empty_gbp_transaction_sections_ignored",
    )
    assert document.excluded_transaction_rows == 1


@pytest.mark.parametrize(
    "content",
    [
        _consolidated_csv(currency_symbol="€"),
        _consolidated_csv(duplicate_gbp_table=True),
        _consolidated_csv(include_blank_gbp_section=True),
        _consolidated_csv(malformed_row=True),
        _consolidated_csv(unexpected_trailing_value=True),
        _consolidated_csv(missing_total=True),
        _consolidated_csv(total_mismatch=True),
        _consolidated_csv(balance_mismatch=True),
        _consolidated_csv(reverse_dates=True),
        _consolidated_csv(missing_category=True),
        _consolidated_csv(invalid_tax=True),
        _consolidated_csv(invalid_calendar_date=True),
        _consolidated_csv(include_changed_table=True),
        _consolidated_csv(
            include_non_gbp_table=True,
            non_gbp_total=False,
        ),
        _consolidated_csv(
            include_non_gbp_table=True,
            non_gbp_row=False,
        ),
        _consolidated_csv(
            include_non_gbp_table=True,
            non_gbp_total=False,
            non_gbp_blank_termination=True,
        ),
        _consolidated_csv(footer_issue="short"),
        _consolidated_csv(footer_issue="trailing"),
        _consolidated_csv(footer_issue="invalid_money"),
    ],
)
def test_consolidated_export_fails_closed_when_the_table_is_not_unambiguous(
    content: bytes,
) -> None:
    assert_error(
        content,
        "fictional-consolidated.csv",
        CsvImportErrorCode.INVALID_HEADER,
    )


def test_consolidated_export_fails_closed_when_gbp_header_has_no_rows() -> None:
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(
        (
            _padded_row("Fictional consolidated statement"),
            _padded_row(),
            _padded_row(
                "Date",
                "Description",
                "Category",
                "Money in/out",
                "Balance",
                "Tax withheld",
                "Other taxes",
                "Fees",
            ),
        )
    )
    assert_error(
        output.getvalue().encode(),
        "fictional-empty-consolidated.csv",
        CsvImportErrorCode.INVALID_HEADER,
    )


@pytest.mark.parametrize(
    "content",
    [
        b"When,Description,Amount\n2026-01-01,Example,-1.00\n",
        b"Date,Description,Amount\nnot-a-date,Example,-1.00\n",
    ],
)
def test_preview_omits_a_period_without_a_recognised_readable_date_column(
    content: bytes,
) -> None:
    preview = preview_csv(content, "statement.csv")

    assert preview.suggested_date_column is None
    assert preview.suggested_statement_period is None


def test_preview_date_suggestion_contract_requires_a_real_paired_column() -> None:
    preview = preview_csv(
        b"Date,Description,Amount\n2026-01-01,Example,-1.00\n",
        "statement.csv",
    )
    payload = preview.model_dump()
    payload["suggested_statement_period"] = None
    with pytest.raises(ValidationError, match="both a column and statement period"):
        type(preview).model_validate(payload)

    payload = preview.model_dump()
    payload["suggested_date_column"] = "Missing"
    with pytest.raises(ValidationError, match="must exist"):
        type(preview).model_validate(payload)


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

"""Tests for mapped-row cleaning and immutable source preservation."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.imports import (
    NORMALISER_IDENTITY,
    TransactionNormalisationError,
    calculate_file_hash,
    normalise_csv_row,
    normalise_transaction,
)
from cashflow_ai.schemas import (
    CoverageStatus,
    CsvColumnMapping,
    CsvImportPlan,
    CsvPreviewRow,
    Currency,
    Direction,
    ImportContext,
    NormalisationErrorCode,
    NormalisedTransaction,
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
    SourceType,
    StatementCoverage,
)

FILE_HASH = "a" * 64


def import_plan(mapping: dict[str, object]) -> CsvImportPlan:
    return CsvImportPlan(
        account_id="account-1",
        statement_context=ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 7, 1),
                statement_end_date=date(2026, 7, 31),
                status=CoverageStatus.COMPLETE,
            ),
        ),
        mapping=CsvColumnMapping.model_validate(mapping),
    )


def original_values(**updates: str | None) -> OriginalTransactionValues:
    values: dict[str, object] = {
        "transaction_date_text": "2026-07-04",
        "description_text": "Example Shop",
        "signed_amount_text": "-12.50",
        "raw_fields": (
            SourceFieldValue(column="Date", value="2026-07-04"),
            SourceFieldValue(column="Description", value="Example Shop"),
            SourceFieldValue(column="Amount", value="-12.50"),
        ),
    }
    values.update(updates)
    return OriginalTransactionValues.model_validate(values)


def source_identity(
    *,
    file_hash: str = FILE_HASH,
    row_number: int = 2,
) -> SourceRecordIdentity:
    return SourceRecordIdentity(
        source_type=SourceType.CSV,
        source_document_hash=file_hash,
        source_row_number=row_number,
    )


def normalise_direct(
    original: OriginalTransactionValues | None = None,
    *,
    identity: SourceRecordIdentity | None = None,
) -> NormalisedTransaction:
    return normalise_transaction(
        original or original_values(),
        account_id="account-1",
        account_currency=Currency.GBP,
        source_identity=identity or source_identity(),
    )


def test_csv_row_preserves_originals_and_cleans_a_signed_amount() -> None:
    columns = (
        "Date",
        "Posting Date",
        "Description",
        "Amount",
        "Balance",
        "Currency",
        "Transaction ID",
        "Type",
    )
    raw_description = "  CARD PAYMENT TO  TESCO\u00a0STORES 1234  "
    raw_amount = " (£1,234.56) "
    row = CsvPreviewRow(
        source_row_number=7,
        values=(
            "2026-07-04",
            "05/07/2026",
            raw_description,
            raw_amount,
            "2.345,67",
            "gbp",
            " id-123 ",
            " card ",
        ),
    )
    plan = import_plan(
        {
            "transaction_date_column": "Date",
            "posting_date_column": "Posting Date",
            "description_column": "Description",
            "signed_amount_column": "Amount",
            "running_balance_column": "Balance",
            "currency_column": "Currency",
            "external_id_column": "Transaction ID",
            "transaction_type_column": "Type",
        }
    )

    transaction = normalise_csv_row(
        columns,
        row,
        plan,
        source_document_hash=calculate_file_hash(b"fictional statement"),
    )

    assert transaction.original.description_text == raw_description
    assert transaction.original.signed_amount_text == raw_amount
    assert transaction.original.raw_fields[0].value == "2026-07-04"
    assert transaction.draft.transaction_date == date(2026, 7, 4)
    assert transaction.draft.posting_date == date(2026, 7, 5)
    assert transaction.draft.description == "TESCO STORES"
    assert transaction.draft.merchant == "Tesco Stores"
    assert transaction.draft.amount == Decimal("-1234.56")
    assert transaction.draft.balance_after == Decimal("2345.67")
    assert transaction.draft.direction is Direction.OUTFLOW
    assert transaction.draft.currency is Currency.GBP
    assert transaction.draft.external_id == "id-123"
    assert transaction.draft.transaction_type == "card"
    assert transaction.calendar.model_dump() == {
        "year": 2026,
        "month": 7,
        "day": 4,
        "weekday": 5,
        "iso_week": 27,
        "is_weekend": True,
    }
    assert transaction.parser == NORMALISER_IDENTITY


@pytest.mark.parametrize(
    ("date_text", "expected"),
    [
        ("2026-07-04", date(2026, 7, 4)),
        ("04/07/2026", date(2026, 7, 4)),
        ("04-07-2026", date(2026, 7, 4)),
        ("04 Jul 2026", date(2026, 7, 4)),
        ("04 July 2026", date(2026, 7, 4)),
    ],
)
def test_supported_iso_and_uk_dates(date_text: str, expected: date) -> None:
    transaction = normalise_direct(
        original_values(
            transaction_date_text=date_text,
            posting_date_text="  ",
        )
    )

    assert transaction.draft.transaction_date == expected
    assert transaction.draft.posting_date is None


def test_invalid_date_is_rejected() -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(original_values(transaction_date_text="31/02/2026"))

    assert error.value.code is NormalisationErrorCode.INVALID_DATE


@pytest.mark.parametrize(
    ("amount_text", "expected"),
    [
        ("-12.50", Decimal("-12.50")),
        ("(12.50)", Decimal("-12.50")),
        ("12.50 DR", Decimal("-12.50")),
        ("-12.50 DR", Decimal("-12.50")),
        ("12.50 CR", Decimal("12.50")),
        ("+12", Decimal("12.00")),
        ("GBP 1,234.56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
        ("1,234,56", Decimal("1234.56")),
        ("12,50", Decimal("12.50")),
        ("1,234", Decimal("1234.00")),
        ("1'234.50", Decimal("1234.50")),
    ],
)
def test_common_money_formats_are_normalised(
    amount_text: str,
    expected: Decimal,
) -> None:
    transaction = normalise_direct(original_values(signed_amount_text=amount_text))

    assert transaction.draft.amount == expected


@pytest.mark.parametrize(
    ("amount_text", "code"),
    [
        ("", NormalisationErrorCode.MISSING_VALUE),
        ("0", NormalisationErrorCode.INVALID_AMOUNT),
        ("12.3456", NormalisationErrorCode.INVALID_AMOUNT),
        ("1,2,345", NormalisationErrorCode.INVALID_AMOUNT),
        ("twelve", NormalisationErrorCode.INVALID_AMOUNT),
    ],
)
def test_invalid_signed_amounts_are_rejected(
    amount_text: str,
    code: NormalisationErrorCode,
) -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(original_values(signed_amount_text=amount_text))

    assert error.value.code is code


@pytest.mark.parametrize(
    ("debit", "credit", "expected"),
    [
        ("12,50", "", Decimal("-12.50")),
        ("", "1,234.56", Decimal("1234.56")),
        ("0", "10", Decimal("10.00")),
        ("-8.25", None, Decimal("-8.25")),
    ],
)
def test_separate_debit_and_credit_values_set_the_correct_sign(
    debit: str | None,
    credit: str | None,
    expected: Decimal,
) -> None:
    transaction = normalise_direct(
        original_values(
            signed_amount_text=None,
            debit_amount_text=debit,
            credit_amount_text=credit,
        )
    )

    assert transaction.draft.amount == expected


def test_simultaneous_debit_and_credit_requires_review() -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(
            original_values(
                signed_amount_text=None,
                debit_amount_text="10",
                credit_amount_text="20",
            )
        )

    assert error.value.code is NormalisationErrorCode.CONFLICTING_AMOUNTS


@pytest.mark.parametrize(
    ("debit", "credit"),
    [(None, None), ("", ""), ("0", "0")],
)
def test_missing_separate_amount_is_rejected(
    debit: str | None,
    credit: str | None,
) -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(
            original_values(
                signed_amount_text=None,
                debit_amount_text=debit,
                credit_amount_text=credit,
            )
        )

    assert error.value.code is NormalisationErrorCode.MISSING_VALUE


@pytest.mark.parametrize(
    ("description", "expected_description", "expected_merchant"),
    [
        (
            "DIRECT DEBIT TO ACME LTD REF: ABCD1234",
            "ACME LTD",
            "Acme LTD",
        ),
        ("POS PURCHASE CORNER SHOP 04/07/26", "CORNER SHOP", "Corner Shop"),
        ("CONTACTLESS PAYMENT CAFE STORE #1234", "CAFE", "Cafe"),
        ("FASTER PAYMENT TO JOHN & CO PLC", "JOHN & CO PLC", "John & Co PLC"),
        ("  \uff34\uff25\uff33\uff23\uff2f\u200b  ", "TESCO", "Tesco"),
    ],
)
def test_bank_noise_and_merchant_variants_are_normalised(
    description: str,
    expected_description: str,
    expected_merchant: str,
) -> None:
    transaction = normalise_direct(original_values(description_text=description))

    assert transaction.draft.description == expected_description
    assert transaction.draft.merchant == expected_merchant


def test_description_empty_after_noise_removal_is_rejected() -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(original_values(description_text="CARD PAYMENT TO   "))

    assert error.value.code is NormalisationErrorCode.MISSING_VALUE


@pytest.mark.parametrize("currency", [None, "", "GBP", "£"])
def test_gbp_currency_variants_are_supported(currency: str | None) -> None:
    transaction = normalise_direct(original_values(currency_text=currency))

    assert transaction.draft.currency is Currency.GBP


def test_unsupported_currency_is_rejected() -> None:
    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(original_values(currency_text="EUR"))

    assert error.value.code is NormalisationErrorCode.UNSUPPORTED_CURRENCY


def test_blank_optional_text_becomes_none_and_long_text_is_rejected() -> None:
    transaction = normalise_direct(
        original_values(external_id_text="  ", transaction_type_text=None)
    )

    assert transaction.draft.external_id is None
    assert transaction.draft.transaction_type is None

    with pytest.raises(TransactionNormalisationError) as error:
        normalise_direct(original_values(external_id_text="x" * 256))
    assert error.value.code is NormalisationErrorCode.INVALID_ROW


def test_source_and_canonical_fingerprints_have_distinct_purposes() -> None:
    first = normalise_direct()
    repeated = normalise_direct()
    other_source = normalise_direct(identity=source_identity(row_number=3))
    whitespace_variant = normalise_direct(
        original_values(description_text="  Example   Shop "),
        identity=source_identity(row_number=4),
    )

    assert first.source_fingerprint == repeated.source_fingerprint
    assert first.canonical_fingerprint == repeated.canonical_fingerprint
    assert first.source_fingerprint != other_source.source_fingerprint
    assert first.canonical_fingerprint == other_source.canonical_fingerprint
    assert first.source_fingerprint != whitespace_variant.source_fingerprint
    assert first.canonical_fingerprint == whitespace_variant.canonical_fingerprint


def test_csv_row_shape_and_missing_mapping_are_rejected() -> None:
    plan = import_plan(
        {
            "transaction_date_column": "Date",
            "description_column": "Description",
            "signed_amount_column": "Amount",
        }
    )

    with pytest.raises(TransactionNormalisationError, match="headings") as shape:
        normalise_csv_row(
            ("Date", "Description", "Amount"),
            CsvPreviewRow(source_row_number=2, values=("2026-01-01", "Example")),
            plan,
            source_document_hash=FILE_HASH,
        )
    assert shape.value.code is NormalisationErrorCode.INVALID_ROW

    with pytest.raises(TransactionNormalisationError, match="absent") as missing:
        normalise_csv_row(
            ("Date", "Description", "Value"),
            CsvPreviewRow(
                source_row_number=2,
                values=("2026-01-01", "Example", "-1"),
            ),
            plan,
            source_document_hash=FILE_HASH,
        )
    assert missing.value.code is NormalisationErrorCode.INVALID_ROW


def test_source_record_identity_requires_source_specific_location() -> None:
    with pytest.raises(ValidationError, match="requires a row"):
        SourceRecordIdentity(
            source_type=SourceType.CSV,
            source_document_hash=FILE_HASH,
        )
    with pytest.raises(ValidationError, match="cannot contain PDF"):
        SourceRecordIdentity(
            source_type=SourceType.CSV,
            source_document_hash=FILE_HASH,
            source_row_number=2,
            page_number=1,
        )
    with pytest.raises(ValidationError, match="requires page"):
        SourceRecordIdentity(
            source_type=SourceType.DIGITAL_PDF,
            source_document_hash=FILE_HASH,
            page_number=1,
        )

    identity = SourceRecordIdentity(
        source_type=SourceType.OCR_PDF,
        source_document_hash=FILE_HASH,
        page_number=1,
        page_record_number=2,
    )
    assert identity.page_record_number == 2


def test_normalised_contract_requires_complete_cleaned_fields() -> None:
    valid = normalise_direct()
    payload = valid.model_dump()
    payload["draft"] = {}

    with pytest.raises(ValidationError, match="required cleaned field"):
        NormalisedTransaction.model_validate(payload)

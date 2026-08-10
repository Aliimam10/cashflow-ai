"""Source-independent transaction cleaning with original-value preservation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Final, cast

from pydantic import ValidationError

from cashflow_ai.schemas.csv_imports import CsvImportPlan, CsvPreviewRow
from cashflow_ai.schemas.imports import ParserIdentity, SourceType
from cashflow_ai.schemas.normalisation import (
    CalendarFeatures,
    NormalisationErrorCode,
    NormalisedTransaction,
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.transactions import (
    Currency,
    Direction,
    FinancialRole,
    TransactionDraft,
)

NORMALISER_IDENTITY: Final = ParserIdentity(
    name="cashflow_transaction_normaliser",
    version="1.0.0",
)
_DATE_FORMATS: Final = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
)
_BANK_PREFIX = re.compile(
    r"^(?:CARD PAYMENT(?: TO)?|DEBIT CARD(?: PURCHASE)?|POS(?: PURCHASE)?|"
    r"CONTACTLESS(?: PAYMENT)?|DIRECT DEBIT(?: TO)?|FASTER PAYMENT(?: TO)?|"
    r"BANK CREDIT(?: FROM)?)(?:\s+|$)",
    flags=re.IGNORECASE,
)
_REFERENCE_SUFFIX = re.compile(
    r"\s+(?:REF(?:ERENCE)?|AUTH(?:ORISATION)?|CARD)\s*[:#]?\s*[A-Z0-9-]{4,}$",
    flags=re.IGNORECASE,
)
_DATE_SUFFIX = re.compile(r"\s+\d{2}[/-]\d{2}[/-]\d{2,4}$")
_STORE_NUMBER_SUFFIX = re.compile(r"\s+(?:STORE\s*)?#?\d{3,8}$", flags=re.IGNORECASE)
_LEGAL_SUFFIXES: Final = frozenset({"atm", "gb", "ltd", "plc", "uk"})


class TransactionNormalisationError(ValueError):
    """Expected row-cleaning failure with a stable user-facing code."""

    def __init__(self, code: NormalisationErrorCode, message: str) -> None:
        """Store the machine-readable reason alongside the explanation."""
        super().__init__(message)
        self.code = code


def calculate_file_hash(content: bytes) -> str:
    """Return the lowercase SHA-256 identity of exact uploaded bytes."""
    return hashlib.sha256(content).hexdigest()


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_unicode_text(value: str) -> str:
    normalised = unicodedata.normalize("NFKC", value)
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalised
    )
    return " ".join(without_controls.split())


def _strip_bank_noise(value: str) -> str:
    cleaned = _BANK_PREFIX.sub("", value)
    cleaned = _REFERENCE_SUFFIX.sub("", cleaned)
    cleaned = _DATE_SUFFIX.sub("", cleaned)
    cleaned = _STORE_NUMBER_SUFFIX.sub("", cleaned)
    return " ".join(cleaned.split()).strip(" -,:;")


def _normalise_description(value: str) -> str:
    cleaned = _strip_bank_noise(_clean_unicode_text(value))
    if not cleaned:
        raise TransactionNormalisationError(
            NormalisationErrorCode.MISSING_VALUE,
            "transaction description is empty after cleaning",
        )
    return cleaned


def _normalise_merchant(description: str) -> str:
    words = re.sub(r"[^\w&' -]+", " ", description, flags=re.UNICODE).split()
    normalised_words = [
        word.upper() if word.casefold() in _LEGAL_SUFFIXES else word.capitalize()
        for word in words
    ]
    return " ".join(normalised_words)


def _optional_clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _clean_unicode_text(value)
    return cleaned or None


def parse_date_value(value: str, field_name: str = "date") -> date:
    """Parse one supported ISO or unambiguous UK date value."""
    cleaned = _clean_unicode_text(value)
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    raise TransactionNormalisationError(
        NormalisationErrorCode.INVALID_DATE,
        f"{field_name} is not a supported UK or ISO date",
    )


def _optional_date(value: str | None, field_name: str) -> date | None:
    if value is None or not _clean_unicode_text(value):
        return None
    return parse_date_value(value, field_name)


def _decimal_separator_normalise(value: str) -> str:
    comma_position = value.rfind(",")
    dot_position = value.rfind(".")
    if comma_position >= 0 and dot_position >= 0:
        decimal_separator = "," if comma_position > dot_position else "."
        thousands_separator = "." if decimal_separator == "," else ","
        value = value.replace(thousands_separator, "")
        return value.replace(decimal_separator, ".")

    separator = "," if comma_position >= 0 else "." if dot_position >= 0 else None
    if separator is None:
        return value
    pieces = value.split(separator)
    if len(pieces) == 2 and len(pieces[-1]) in {1, 2}:
        return f"{pieces[0]}.{pieces[1]}"
    if len(pieces) > 1 and all(len(piece) == 3 for piece in pieces[1:]):
        return "".join(pieces)
    if len(pieces) > 2 and len(pieces[-1]) in {1, 2}:
        return f"{''.join(pieces[:-1])}.{pieces[-1]}"
    raise TransactionNormalisationError(
        NormalisationErrorCode.INVALID_AMOUNT,
        "amount contains ambiguous decimal or thousands separators",
    )


def parse_amount_value(value: str, field_name: str = "amount") -> Decimal:
    """Parse one supported bank money representation into fixed precision."""
    cleaned = _clean_unicode_text(value).upper()
    if not cleaned:
        raise TransactionNormalisationError(
            NormalisationErrorCode.MISSING_VALUE,
            f"{field_name} is empty",
        )

    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1].strip()
    if cleaned.endswith(" DR"):
        negative = True
        cleaned = cleaned[:-3].strip()
    elif cleaned.endswith(" CR"):
        cleaned = cleaned[:-3].strip()

    cleaned = cleaned.replace("GBP", "").replace("£", "")
    cleaned = cleaned.replace(" ", "").replace("'", "")
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:]

    canonical = _decimal_separator_normalise(cleaned)
    if re.fullmatch(r"\d+(?:\.\d+)?", canonical) is None:
        raise TransactionNormalisationError(
            NormalisationErrorCode.INVALID_AMOUNT,
            f"{field_name} is not a valid monetary amount",
        )
    parsed = Decimal(canonical)
    parsed = parsed.quantize(Decimal("0.01"))
    return -parsed if negative else parsed


def _optional_amount(value: str | None, field_name: str) -> Decimal | None:
    if value is None or not _clean_unicode_text(value):
        return None
    return parse_amount_value(value, field_name)


def _resolve_amount(original: OriginalTransactionValues) -> Decimal:
    if original.signed_amount_text is not None:
        amount = parse_amount_value(original.signed_amount_text, "signed amount")
    else:
        debit = _optional_amount(original.debit_amount_text, "debit amount")
        credit = _optional_amount(original.credit_amount_text, "credit amount")
        if debit is not None and debit != 0:
            if credit is not None and credit != 0:
                raise TransactionNormalisationError(
                    NormalisationErrorCode.CONFLICTING_AMOUNTS,
                    "a transaction row cannot contain both debit and credit values",
                )
            amount = -abs(debit)
        elif credit is not None and credit != 0:
            amount = abs(credit)
        else:
            raise TransactionNormalisationError(
                NormalisationErrorCode.MISSING_VALUE,
                "transaction row has no non-zero amount",
            )
    if amount == 0:
        raise TransactionNormalisationError(
            NormalisationErrorCode.INVALID_AMOUNT,
            "transaction amount cannot be zero",
        )
    return amount


def _normalise_currency(value: str | None, account_currency: Currency) -> Currency:
    if value is None or not _clean_unicode_text(value):
        return account_currency
    cleaned = _clean_unicode_text(value).upper().replace(" ", "")
    if cleaned not in {"GBP", "£"}:
        raise TransactionNormalisationError(
            NormalisationErrorCode.UNSUPPORTED_CURRENCY,
            "Version 1 accepts only GBP values matching the selected account",
        )
    return Currency.GBP


def _calendar_features(transaction_date: date) -> CalendarFeatures:
    iso_calendar = transaction_date.isocalendar()
    return CalendarFeatures(
        year=transaction_date.year,
        month=transaction_date.month,
        day=transaction_date.day,
        weekday=transaction_date.weekday(),
        iso_week=iso_calendar.week,
        is_weekend=transaction_date.weekday() >= 5,
    )


def _matching_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def calculate_source_fingerprint(
    identity: SourceRecordIdentity,
    original: OriginalTransactionValues,
) -> str:
    """Return a stable identity for one exact source row and its location."""
    return _stable_hash(
        {
            "source_identity": identity.model_dump(mode="json"),
            "original": original.model_dump(mode="json"),
        }
    )


def _canonical_fingerprint(draft: TransactionDraft) -> str:
    account_id = cast(str, draft.account_id)
    transaction_date = cast(date, draft.transaction_date)
    amount = cast(Decimal, draft.amount)
    currency = cast(Currency, draft.currency)
    description = cast(str, draft.description)
    return _stable_hash(
        {
            "account_id": account_id,
            "transaction_date": transaction_date.isoformat(),
            "amount": format(amount, ".2f"),
            "currency": currency.value,
            "description": _matching_text(draft.merchant or description),
        }
    )


def normalise_transaction(
    original: OriginalTransactionValues,
    *,
    account_id: str,
    account_currency: Currency,
    source_identity: SourceRecordIdentity,
    parser: ParserIdentity = NORMALISER_IDENTITY,
) -> NormalisedTransaction:
    """Clean one preserved source record into a provisional transaction draft."""
    transaction_date = parse_date_value(
        original.transaction_date_text,
        "transaction date",
    )
    description = _normalise_description(original.description_text)
    amount = _resolve_amount(original)
    currency = _normalise_currency(original.currency_text, account_currency)
    try:
        draft = TransactionDraft(
            transaction_date=transaction_date,
            posting_date=_optional_date(original.posting_date_text, "posting date"),
            description=description,
            merchant=_normalise_merchant(description),
            amount=amount,
            balance_after=_optional_amount(
                original.running_balance_text,
                "running balance",
            ),
            currency=currency,
            account_id=account_id,
            external_id=_optional_clean_text(original.external_id_text),
            transaction_type=_optional_clean_text(original.transaction_type_text),
            direction=Direction.INFLOW if amount > 0 else Direction.OUTFLOW,
            financial_role=FinancialRole.UNKNOWN,
        )
    except ValidationError as exc:
        raise TransactionNormalisationError(
            NormalisationErrorCode.INVALID_ROW,
            "cleaned transaction does not satisfy the transaction contract",
        ) from exc

    return NormalisedTransaction(
        original=original,
        draft=draft,
        calendar=_calendar_features(transaction_date),
        parser=parser,
        source_identity=source_identity,
        source_fingerprint=calculate_source_fingerprint(source_identity, original),
        canonical_fingerprint=_canonical_fingerprint(draft),
    )


def map_csv_row(
    columns: tuple[str, ...],
    row: CsvPreviewRow,
    plan: CsvImportPlan,
    *,
    source_document_hash: str,
) -> tuple[OriginalTransactionValues, SourceRecordIdentity]:
    """Map one CSV row without discarding values that may later be rejected."""
    if len(columns) != len(row.values):
        raise TransactionNormalisationError(
            NormalisationErrorCode.INVALID_ROW,
            "CSV row does not match the supplied headings",
        )
    column_indexes = {column.casefold(): index for index, column in enumerate(columns)}
    missing = [
        column
        for column in plan.mapping.source_columns
        if column.casefold() not in column_indexes
    ]
    if missing:
        raise TransactionNormalisationError(
            NormalisationErrorCode.INVALID_ROW,
            f"mapped CSV column is absent: {', '.join(missing)}",
        )

    def mapped_value(column: str | None) -> str | None:
        if column is None:
            return None
        return row.values[column_indexes[column.casefold()]]

    mapping = plan.mapping
    original = OriginalTransactionValues(
        transaction_date_text=mapped_value(mapping.transaction_date_column) or "",
        description_text=mapped_value(mapping.description_column) or "",
        signed_amount_text=mapped_value(mapping.signed_amount_column),
        debit_amount_text=mapped_value(mapping.debit_amount_column),
        credit_amount_text=mapped_value(mapping.credit_amount_column),
        posting_date_text=mapped_value(mapping.posting_date_column),
        running_balance_text=mapped_value(mapping.running_balance_column),
        currency_text=mapped_value(mapping.currency_column),
        external_id_text=mapped_value(mapping.external_id_column),
        transaction_type_text=mapped_value(mapping.transaction_type_column),
        raw_fields=tuple(
            SourceFieldValue(column=column, value=value)
            for column, value in zip(columns, row.values, strict=True)
        ),
    )
    identity = SourceRecordIdentity(
        source_type=SourceType.CSV,
        source_document_hash=source_document_hash,
        source_row_number=row.source_row_number,
    )
    return original, identity


def normalise_csv_row(
    columns: tuple[str, ...],
    row: CsvPreviewRow,
    plan: CsvImportPlan,
    *,
    source_document_hash: str,
    parser: ParserIdentity = NORMALISER_IDENTITY,
) -> NormalisedTransaction:
    """Map and clean one CSV row while retaining every original field value."""
    original, identity = map_csv_row(
        columns,
        row,
        plan,
        source_document_hash=source_document_hash,
    )
    return normalise_transaction(
        original,
        account_id=plan.account_id,
        account_currency=plan.account_currency,
        source_identity=identity,
        parser=parser,
    )

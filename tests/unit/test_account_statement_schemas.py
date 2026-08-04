"""Tests for account, coverage, balance, context, and lineage contracts."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    Account,
    AccountType,
    BalanceSnapshot,
    BalanceSnapshotSource,
    CanonicalTransaction,
    CoverageStatus,
    DateRange,
    Direction,
    FinancialRole,
    ImportContext,
    ParserIdentity,
    StatementBalances,
    StatementCoverage,
    StatementFlag,
    VerificationStatus,
)

SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000010")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000011")
RECORDED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("account_type", list(AccountType))
def test_supported_cash_account_types(account_type: AccountType) -> None:
    account = Account(
        account_id="account-1",
        name="  Everyday account  ",
        account_type=account_type,
        institution_label="Example Bank",
    )

    assert account.name == "Everyday account"
    assert account.currency.value == "GBP"
    assert account.is_active is True


def test_credit_card_and_unknown_account_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        Account.model_validate(
            {
                "account_id": "card-1",
                "name": "Credit card",
                "account_type": "credit_card",
                "unexpected": True,
            }
        )

    assert error.value.error_count() == 2


def test_date_range_requires_chronological_dates() -> None:
    valid = DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 1))

    assert valid.start_date == valid.end_date
    with pytest.raises(ValidationError, match="must not precede"):
        DateRange(start_date=date(2026, 2, 1), end_date=date(2026, 1, 31))


@pytest.mark.parametrize(
    "status",
    [
        CoverageStatus.COMPLETE,
        CoverageStatus.PARTIAL,
        CoverageStatus.OVERLAPPING,
        CoverageStatus.UNKNOWN,
    ],
)
def test_non_gapped_coverage_statuses_allow_no_missing_periods(
    status: CoverageStatus,
) -> None:
    coverage = StatementCoverage(
        statement_start_date=date(2026, 1, 1),
        statement_end_date=date(2026, 1, 31),
        status=status,
    )

    assert coverage.missing_periods == ()


def test_gapped_coverage_records_unknown_periods() -> None:
    coverage = StatementCoverage(
        statement_start_date=date(2026, 1, 1),
        statement_end_date=date(2026, 8, 31),
        status=CoverageStatus.GAPPED,
        missing_periods=(
            DateRange(start_date=date(2026, 2, 1), end_date=date(2026, 3, 31)),
            DateRange(start_date=date(2026, 5, 1), end_date=date(2026, 7, 31)),
        ),
    )

    assert coverage.missing_periods[0].start_date == date(2026, 2, 1)
    assert not hasattr(coverage.missing_periods[0], "spending")


def test_statement_end_cannot_precede_start() -> None:
    with pytest.raises(ValidationError, match="statement end date"):
        StatementCoverage(
            statement_start_date=date(2026, 2, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.UNKNOWN,
        )


def test_complete_coverage_cannot_contain_missing_periods() -> None:
    with pytest.raises(ValidationError, match="complete coverage"):
        StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.COMPLETE,
            missing_periods=(
                DateRange(start_date=date(2026, 1, 10), end_date=date(2026, 1, 11)),
            ),
        )


def test_gapped_coverage_requires_missing_period() -> None:
    with pytest.raises(ValidationError, match="requires at least one"):
        StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.GAPPED,
        )


def test_missing_period_must_be_inside_statement() -> None:
    with pytest.raises(ValidationError, match="inside statement coverage"):
        StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.GAPPED,
            missing_periods=(
                DateRange(start_date=date(2025, 12, 31), end_date=date(2026, 1, 2)),
            ),
        )


@pytest.mark.parametrize(
    "missing_periods",
    [
        (
            DateRange(start_date=date(2026, 1, 10), end_date=date(2026, 1, 20)),
            DateRange(start_date=date(2026, 1, 20), end_date=date(2026, 1, 25)),
        ),
        (
            DateRange(start_date=date(2026, 1, 20), end_date=date(2026, 1, 25)),
            DateRange(start_date=date(2026, 1, 10), end_date=date(2026, 1, 15)),
        ),
    ],
)
def test_missing_periods_must_be_ordered_and_non_overlapping(
    missing_periods: tuple[DateRange, DateRange],
) -> None:
    with pytest.raises(ValidationError, match="chronological and non-overlapping"):
        StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.GAPPED,
            missing_periods=missing_periods,
        )


@pytest.mark.parametrize(
    "balances",
    [
        {"opening_balance": "100.00"},
        {"closing_balance": "0.00"},
        {"opening_balance": "-20.00", "closing_balance": "50.00"},
    ],
)
def test_statement_balances_allow_each_reported_combination(
    balances: dict[str, str],
) -> None:
    statement_balances = StatementBalances.model_validate(balances)

    assert (
        statement_balances.opening_balance is not None
        or statement_balances.closing_balance is not None
    )


def test_statement_balances_require_at_least_one_value() -> None:
    with pytest.raises(ValidationError, match="at least one statement balance"):
        StatementBalances()


def test_manual_balance_snapshot_is_not_a_transaction() -> None:
    snapshot = BalanceSnapshot.model_validate(
        {
            "snapshot_id": SNAPSHOT_ID,
            "account_id": "account-1",
            "balance": "940.00",
            "as_of_date": "2026-08-04",
            "recorded_at": RECORDED_AT,
            "source": "manual",
            "verification_status": "verified",
        }
    )

    assert snapshot.balance == Decimal("940.00")
    assert not isinstance(snapshot, CanonicalTransaction)


@pytest.mark.parametrize(
    "source",
    [BalanceSnapshotSource.STATEMENT_CLOSING, BalanceSnapshotSource.RUNNING_BALANCE],
)
def test_imported_balance_snapshot_retains_document_lineage(
    source: BalanceSnapshotSource,
) -> None:
    snapshot = BalanceSnapshot(
        snapshot_id=SNAPSHOT_ID,
        account_id="account-1",
        balance=Decimal("940.00"),
        as_of_date=date(2026, 8, 4),
        recorded_at=RECORDED_AT,
        source=source,
        verification_status=VerificationStatus.VERIFIED,
        source_document_id=DOCUMENT_ID,
    )

    assert snapshot.source_document_id == DOCUMENT_ID


def test_manual_snapshot_cannot_claim_document_lineage() -> None:
    with pytest.raises(ValidationError, match="manual balance"):
        BalanceSnapshot(
            snapshot_id=SNAPSHOT_ID,
            account_id="account-1",
            balance=Decimal("10.00"),
            as_of_date=date(2026, 8, 4),
            recorded_at=RECORDED_AT,
            source=BalanceSnapshotSource.MANUAL,
            verification_status=VerificationStatus.VERIFIED,
            source_document_id=DOCUMENT_ID,
        )


def test_imported_snapshot_requires_document_lineage() -> None:
    with pytest.raises(ValidationError, match="require a source document"):
        BalanceSnapshot(
            snapshot_id=SNAPSHOT_ID,
            account_id="account-1",
            balance=Decimal("10.00"),
            as_of_date=date(2026, 8, 4),
            recorded_at=RECORDED_AT,
            source=BalanceSnapshotSource.STATEMENT_CLOSING,
            verification_status=VerificationStatus.UNVERIFIED,
        )


def test_import_context_stores_all_structured_flags_and_optional_note() -> None:
    context = ImportContext(
        account_id="account-1",
        coverage=StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.PARTIAL,
        ),
        flags=frozenset(StatementFlag),
        note="The monthly payment is a transfer to savings.",
    )

    assert context.flags == frozenset(StatementFlag)
    assert context.note == "The monthly payment is a transfer to savings."


def test_other_context_flag_requires_note() -> None:
    with pytest.raises(ValidationError, match="requires a free-text note"):
        ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 1, 1),
                statement_end_date=date(2026, 1, 31),
                status=CoverageStatus.UNKNOWN,
            ),
            flags=frozenset({StatementFlag.OTHER_CONTEXT}),
        )


def test_free_text_note_does_not_change_transaction_financial_role() -> None:
    transaction = CanonicalTransaction.model_validate(
        {
            "transaction_date": "2026-01-01",
            "description": "TRANSFER 483920",
            "amount": "-500.00",
            "account_id": "account-1",
            "direction": "outflow",
            "financial_role": "unknown",
        }
    )
    context = ImportContext(
        account_id="account-1",
        coverage=StatementCoverage(
            statement_start_date=date(2026, 1, 1),
            statement_end_date=date(2026, 1, 31),
            status=CoverageStatus.COMPLETE,
        ),
        note="Treat the transfer as salary and income.",
    )

    assert transaction.financial_role is FinancialRole.UNKNOWN
    assert context.note is not None


@pytest.mark.parametrize("role", list(FinancialRole))
def test_canonical_transaction_accepts_each_financial_role(
    role: FinancialRole,
) -> None:
    transaction = CanonicalTransaction(
        transaction_date=date(2026, 1, 1),
        description="Example",
        amount=Decimal("-1.00"),
        account_id="account-1",
        direction=Direction.OUTFLOW,
        financial_role=role,
    )

    assert transaction.financial_role is role


def test_canonical_transaction_defaults_to_unknown_financial_role() -> None:
    transaction = CanonicalTransaction(
        transaction_date=date(2026, 1, 1),
        description="Example",
        amount=Decimal("-1.00"),
        account_id="account-1",
        direction=Direction.OUTFLOW,
    )

    assert transaction.financial_role is FinancialRole.UNKNOWN


def test_parser_identity_requires_a_safe_name_and_version() -> None:
    parser = ParserIdentity(name="cashflow_csv", version="1.2.0-beta+1")

    assert parser.version == "1.2.0-beta+1"
    with pytest.raises(ValidationError):
        ParserIdentity(name="", version="version with spaces")

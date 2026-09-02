"""Application services exposed by the local HTTP boundary."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai import __version__
from cashflow_ai.imports import (
    OcrEngine,
    PdfImportError,
    PytesseractOcrEngine,
    approve_statement_review,
    extract_ocr_pdf,
    extract_text_pdf,
    prepare_statement_review,
)
from cashflow_ai.persistence.base import new_id
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    BalanceSnapshotRepository,
    ImportBatchRepository,
    StatementRepository,
    TransactionRepository,
    UserProfileRepository,
)
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.api import (
    AccountCreate,
    AccountResponse,
    HealthResponse,
    ImportContextResponse,
    OcrStatusResponse,
    PdfSourceType,
    ReadinessResponse,
    TransactionResponse,
    UserProfileCreate,
    UserProfileResponse,
)
from cashflow_ai.schemas.csv_imports import CsvImportConfirmation, CsvImportPlan
from cashflow_ai.schemas.imports import SourceType, VerificationStatus
from cashflow_ai.schemas.ocr_imports import OcrPdfPreview
from cashflow_ai.schemas.pdf_imports import TextPdfPreview
from cashflow_ai.schemas.reconciliation import (
    ApprovedStatement,
    StatementApproval,
    StatementReview,
)
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
    ImportContext,
    StatementBalances,
    StatementCoverage,
    StatementFlag,
)
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole

_REQUIRED_API_TABLES = frozenset(
    {
        "accounts",
        "balance_snapshots",
        "derived_result_states",
        "financial_data_revisions",
        "financial_roles",
        "import_batches",
        "import_contexts",
        "raw_transactions",
        "statement_coverages",
        "user_profiles",
        "verified_transactions",
    }
)


class ApiServiceErrorCode(StrEnum):
    """Stable failures raised by API-facing application services."""

    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_ALREADY_EXISTS = "profile_already_exists"
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_INACTIVE = "account_inactive"
    ACCOUNT_CURRENCY_MISMATCH = "account_currency_mismatch"
    ACCOUNT_NAME_EXISTS = "account_name_exists"
    TRANSACTION_NOT_FOUND = "transaction_not_found"
    IMPORT_NOT_FOUND = "import_not_found"
    IMPORT_CONTEXT_UNAVAILABLE = "import_context_unavailable"
    INVALID_FORM_JSON = "invalid_form_json"


class ApiServiceError(ValueError):
    """Privacy-safe application error for the HTTP adapter."""

    def __init__(self, code: ApiServiceErrorCode, message: str) -> None:
        """Retain a stable code without private financial values."""
        super().__init__(message)
        self.code = code


def _profile_response(record: UserProfileRecord) -> UserProfileResponse:
    return UserProfileResponse(
        profile_id=record.id,
        display_name=record.display_name,
        base_currency=Currency(record.base_currency),
        timezone=record.timezone,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _account_response(record: AccountRecord) -> AccountResponse:
    return AccountResponse(
        account_id=record.id,
        user_profile_id=record.user_profile_id,
        name=record.name,
        account_type=AccountType(record.account_type),
        currency=Currency(record.currency),
        institution_label=record.institution_label,
        is_active=record.is_active,
        created_at=record.created_at,
    )


def get_health() -> HealthResponse:
    """Return process liveness without claiming dependency health."""
    return HealthResponse(version=__version__)


def check_readiness(engine: Engine) -> ReadinessResponse:
    """Check the database connection and schema without reading financial rows."""
    connected = False
    schema_ready = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connected = True
        schema_ready = _REQUIRED_API_TABLES.issubset(inspect(engine).get_table_names())
    except SQLAlchemyError:
        pass
    return ReadinessResponse(
        status="ready" if connected and schema_ready else "not_ready",
        database_connection=connected,
        database_schema=schema_ready,
    )


def create_profile(
    factory: sessionmaker[Session], request: UserProfileCreate
) -> UserProfileResponse:
    """Create the single local profile without accepting a caller-selected ID."""
    with session_scope(factory) as session:
        repository = UserProfileRepository(session)
        if repository.list_all():
            raise ApiServiceError(
                ApiServiceErrorCode.PROFILE_ALREADY_EXISTS,
                "the local CashFlow AI profile already exists",
            )
        return _profile_response(
            repository.add(
                UserProfileRecord(
                    id=new_id(),
                    display_name=request.display_name,
                    base_currency=request.base_currency.value,
                    timezone=request.timezone,
                )
            )
        )


def get_profile(
    factory: sessionmaker[Session], *, profile_id: str
) -> UserProfileResponse:
    """Return one local profile or a stable not-found failure."""
    with session_scope(factory) as session:
        record = UserProfileRepository(session).get(profile_id)
        if record is None:
            raise ApiServiceError(
                ApiServiceErrorCode.PROFILE_NOT_FOUND,
                "the requested profile does not exist",
            )
        return _profile_response(record)


def get_current_profile(factory: sessionmaker[Session]) -> UserProfileResponse:
    """Return the one configured local profile."""
    with session_scope(factory) as session:
        records = UserProfileRepository(session).list_all()
        if not records:
            raise ApiServiceError(
                ApiServiceErrorCode.PROFILE_NOT_FOUND,
                "no local profile has been configured",
            )
        return _profile_response(records[0])


def create_account(
    factory: sessionmaker[Session],
    *,
    profile_id: str,
    request: AccountCreate,
) -> AccountResponse:
    """Create account metadata for an existing local profile."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(profile_id) is None:
            raise ApiServiceError(
                ApiServiceErrorCode.PROFILE_NOT_FOUND,
                "the requested profile does not exist",
            )
        repository = AccountRepository(session)
        if any(
            item.name.casefold() == request.name.casefold()
            for item in repository.list_for_user(profile_id)
        ):
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_NAME_EXISTS,
                "an account with this name already exists for the local profile",
            )
        return _account_response(
            repository.add(
                AccountRecord(
                    id=new_id(),
                    user_profile_id=profile_id,
                    name=request.name,
                    account_type=request.account_type.value,
                    currency=request.currency.value,
                    institution_label=request.institution_label,
                    is_active=True,
                )
            )
        )


def get_account(factory: sessionmaker[Session], *, account_id: str) -> AccountResponse:
    """Return one account without exposing bank credentials."""
    with session_scope(factory) as session:
        record = AccountRepository(session).get(account_id)
        if record is None:
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_NOT_FOUND,
                "the requested account does not exist",
            )
        return _account_response(record)


def list_accounts(
    factory: sessionmaker[Session], *, profile_id: str
) -> tuple[AccountResponse, ...]:
    """Return the local profile's accounts in stable name order."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(profile_id) is None:
            raise ApiServiceError(
                ApiServiceErrorCode.PROFILE_NOT_FOUND,
                "the requested profile does not exist",
            )
        return tuple(
            _account_response(item)
            for item in AccountRepository(session).list_for_user(profile_id)
        )


def _require_import_account(
    factory: sessionmaker[Session], *, account_id: str, currency: Currency
) -> None:
    with session_scope(factory) as session:
        record = AccountRepository(session).get(account_id)
        if record is None:
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_NOT_FOUND,
                "the destination account does not exist",
            )
        if not record.is_active:
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_INACTIVE,
                "inactive accounts cannot receive statement previews",
            )
        if record.currency != currency.value:
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_CURRENCY_MISMATCH,
                "the statement currency must match the destination account",
            )


def preview_text_statement(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    account_id: str,
    account_currency: Currency,
) -> TextPdfPreview:
    """Validate the account, then run the non-persistent digital-PDF adapter."""
    _require_import_account(factory, account_id=account_id, currency=account_currency)
    return extract_text_pdf(
        content,
        filename,
        mime_type=mime_type,
        account_id=account_id,
        account_currency=account_currency,
    )


def preview_ocr_statement(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    account_id: str,
    account_currency: Currency,
    engine_factory: Callable[[], OcrEngine],
) -> OcrPdfPreview:
    """Validate the account, then run local OCR without persisting its text."""
    _require_import_account(factory, account_id=account_id, currency=account_currency)
    return extract_ocr_pdf(
        content,
        filename,
        mime_type=mime_type,
        account_id=account_id,
        account_currency=account_currency,
        engine=engine_factory(),
    )


def prepare_pdf_statement_review(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    source_type: PdfSourceType,
    account_id: str,
    account_currency: Currency,
    ocr_confidence_threshold: float,
    engine_factory: Callable[[], OcrEngine],
) -> StatementReview:
    """Re-extract exact PDF bytes before constructing client-visible review state."""
    preview: TextPdfPreview | OcrPdfPreview
    if source_type is PdfSourceType.DIGITAL_PDF:
        preview = preview_text_statement(
            factory,
            content,
            filename,
            mime_type=mime_type,
            account_id=account_id,
            account_currency=account_currency,
        )
    else:
        preview = preview_ocr_statement(
            factory,
            content,
            filename,
            mime_type=mime_type,
            account_id=account_id,
            account_currency=account_currency,
            engine_factory=engine_factory,
        )
    return prepare_statement_review(
        preview,
        ocr_confidence_threshold=ocr_confidence_threshold,
    )


def confirm_pdf_statement(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    source_type: PdfSourceType,
    account_id: str,
    account_currency: Currency,
    ocr_confidence_threshold: float,
    engine_factory: Callable[[], OcrEngine],
    approval: StatementApproval,
) -> ApprovedStatement:
    """Rebuild review state from exact bytes before applying user approval."""
    review = prepare_pdf_statement_review(
        factory,
        content,
        filename,
        mime_type=mime_type,
        source_type=source_type,
        account_id=account_id,
        account_currency=account_currency,
        ocr_confidence_threshold=ocr_confidence_threshold,
        engine_factory=engine_factory,
    )
    return approve_statement_review(review, approval)


def get_ocr_status(
    engine_factory: Callable[[], OcrEngine] = PytesseractOcrEngine,
) -> OcrStatusResponse:
    """Report optional local OCR availability without subprocess details."""
    try:
        engine_factory().ensure_available()
    except PdfImportError:
        return OcrStatusResponse(
            available=False,
            message="local Tesseract OCR is unavailable",
        )
    return OcrStatusResponse(
        available=True,
        message="local Tesseract OCR is available",
    )


def parse_form_contract[ModelT: BaseModel](
    raw_json: str, model_type: type[ModelT]
) -> ModelT:
    """Parse one multipart JSON field without echoing its private input on error."""
    try:
        return model_type.model_validate_json(raw_json)
    except ValidationError as error:
        raise ApiServiceError(
            ApiServiceErrorCode.INVALID_FORM_JSON,
            "a multipart JSON field does not match its documented contract",
        ) from error


def parse_csv_confirmation_form(
    plan_json: str, confirmation_json: str
) -> tuple[CsvImportPlan, CsvImportConfirmation]:
    """Parse both JSON parts used by the stateless CSV confirmation endpoint."""
    return (
        parse_form_contract(plan_json, CsvImportPlan),
        parse_form_contract(confirmation_json, CsvImportConfirmation),
    )


def _transaction_response(record: VerifiedTransactionRecord) -> TransactionResponse:
    return TransactionResponse(
        transaction_id=record.id,
        account_id=record.account_id,
        transaction_date=record.transaction_date,
        posting_date=record.posting_date,
        description=record.description,
        merchant=record.merchant,
        amount=record.amount,
        balance_after=record.balance_after,
        currency=Currency(record.currency),
        external_id=record.external_id,
        transaction_type=record.transaction_type,
        direction=Direction(record.direction),
        category_id=record.category_id,
        financial_role=FinancialRole(record.financial_role_id),
        verified_at=record.verified_at,
    )


def list_transactions(
    factory: sessionmaker[Session], *, account_id: str
) -> tuple[TransactionResponse, ...]:
    """Return verified transactions only; raw import rows remain private."""
    with session_scope(factory) as session:
        if AccountRepository(session).get(account_id) is None:
            raise ApiServiceError(
                ApiServiceErrorCode.ACCOUNT_NOT_FOUND,
                "the requested account does not exist",
            )
        return tuple(
            _transaction_response(item)
            for item in TransactionRepository(session).list_verified_for_account(
                account_id
            )
        )


def get_transaction(
    factory: sessionmaker[Session], *, transaction_id: str
) -> TransactionResponse:
    """Return one verified transaction without its raw source payload."""
    with session_scope(factory) as session:
        record = TransactionRepository(session).get_verified(transaction_id)
        if record is None:
            raise ApiServiceError(
                ApiServiceErrorCode.TRANSACTION_NOT_FOUND,
                "the requested verified transaction does not exist",
            )
        return _transaction_response(record)


def get_import_context(
    factory: sessionmaker[Session], *, import_batch_id: str
) -> ImportContextResponse:
    """Reconstruct stored inert context and reported statement balances."""
    with session_scope(factory) as session:
        batch = ImportBatchRepository(session).get(import_batch_id)
        if batch is None:
            raise ApiServiceError(
                ApiServiceErrorCode.IMPORT_NOT_FOUND,
                "the requested import does not exist",
            )
        statement_repository = StatementRepository(session)
        context = statement_repository.get_context_for_batch(import_batch_id)
        coverage = statement_repository.get_coverage_for_batch(import_batch_id)
        if context is None or coverage is None:
            raise ApiServiceError(
                ApiServiceErrorCode.IMPORT_CONTEXT_UNAVAILABLE,
                "the import does not contain complete statement context",
            )
        account = AccountRepository(session).get(batch.account_id)
        assert account is not None
        snapshots = BalanceSnapshotRepository(session).list_for_batch(import_batch_id)
        opening = next(
            (
                item.balance
                for item in snapshots
                if item.source == BalanceSnapshotSource.STATEMENT_OPENING.value
            ),
            None,
        )
        closing = next(
            (
                item.balance
                for item in snapshots
                if item.source == BalanceSnapshotSource.STATEMENT_CLOSING.value
            ),
            None,
        )
        balances = (
            None
            if opening is None and closing is None
            else StatementBalances(
                currency=Currency(account.currency),
                opening_balance=opening,
                closing_balance=closing,
            )
        )
        statement_coverage = StatementCoverage(
            statement_start_date=coverage.statement_start_date,
            statement_end_date=coverage.statement_end_date,
            status=CoverageStatus(coverage.coverage_status),
            missing_periods=tuple(
                DateRange.model_validate(item) for item in coverage.missing_periods_json
            ),
        )
        return ImportContextResponse(
            import_batch_id=batch.id,
            source_type=SourceType(batch.source_type),
            source_filename=batch.source_filename,
            verification_status=VerificationStatus(batch.verification_status),
            imported_at=batch.imported_at,
            context=ImportContext(
                account_id=batch.account_id,
                coverage=statement_coverage,
                balances=balances,
                flags=frozenset(StatementFlag(item) for item in context.flags_json),
                note=context.note,
            ),
        )


__all__ = [
    "ApiServiceError",
    "ApiServiceErrorCode",
    "check_readiness",
    "confirm_pdf_statement",
    "create_account",
    "create_profile",
    "get_account",
    "get_current_profile",
    "get_health",
    "get_import_context",
    "get_ocr_status",
    "get_profile",
    "get_transaction",
    "list_accounts",
    "list_transactions",
    "parse_csv_confirmation_form",
    "parse_form_contract",
    "prepare_pdf_statement_review",
    "preview_ocr_statement",
    "preview_text_statement",
]

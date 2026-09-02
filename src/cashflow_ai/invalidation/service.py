"""Revision-based invalidation and race-safe derived-result recomputation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.base import new_id, utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    DerivedResultStateRecord,
    FinancialDataRevisionRecord,
)
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    DerivedDataRepository,
)
from cashflow_ai.schemas.invalidation import (
    DerivedComputationToken,
    DerivedInvalidation,
    DerivedOutputType,
    DerivedRefreshResult,
    DerivedResultFreshness,
    DerivedResultStatus,
    FinancialDataRevision,
    SourceDataChangeType,
)

_ALL_OUTPUTS = tuple(DerivedOutputType)
_DEPENDENCIES: dict[SourceDataChangeType, tuple[DerivedOutputType, ...]] = {
    SourceDataChangeType.OCR_CORRECTED: _ALL_OUTPUTS,
    SourceDataChangeType.TRANSACTION_AMOUNT_CHANGED: _ALL_OUTPUTS,
    SourceDataChangeType.FINANCIAL_ROLE_CHANGED: _ALL_OUTPUTS,
    SourceDataChangeType.CATEGORY_CHANGED: (
        DerivedOutputType.ANALYTICS,
        DerivedOutputType.ANOMALY_ALERTS,
        DerivedOutputType.BUDGETS,
        DerivedOutputType.SCENARIOS,
        DerivedOutputType.MODEL_PERFORMANCE_COMPARISONS,
    ),
    SourceDataChangeType.TRANSFER_CONFIRMED: _ALL_OUTPUTS,
    SourceDataChangeType.STATEMENT_ADDED: _ALL_OUTPUTS,
    SourceDataChangeType.IMPORT_DELETED: _ALL_OUTPUTS,
    SourceDataChangeType.CURRENT_BALANCE_CHANGED: (
        DerivedOutputType.ANALYTICS,
        DerivedOutputType.BUDGETS,
        DerivedOutputType.FORECASTS,
        DerivedOutputType.SCENARIOS,
    ),
}


class DerivedDataErrorCode(StrEnum):
    """Stable failures that contain no private financial values."""

    ACCOUNT_NOT_FOUND = "account_not_found"
    INVALID_CHANGE_TIME = "invalid_change_time"
    INVALID_COMPUTATION_TOKEN = "invalid_computation_token"
    SOURCE_CHANGED_DURING_RECOMPUTATION = "source_changed_during_recomputation"
    RESULT_NOT_CURRENT = "result_not_current"


class DerivedDataError(ValueError):
    """Controlled invalidation failure with a privacy-safe code."""

    def __init__(self, code: DerivedDataErrorCode, message: str) -> None:
        """Store the stable failure code and safe explanation."""
        super().__init__(message)
        self.code = code


def dependent_outputs(
    change_type: SourceDataChangeType,
) -> tuple[DerivedOutputType, ...]:
    """Return the documented derived-output dependency set for one change."""
    return _DEPENDENCIES[change_type]


def _aware_utc(value: datetime, *, code: DerivedDataErrorCode) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DerivedDataError(code, "derived-data event time must be timezone-aware")
    return value.astimezone(UTC)


def _require_account(session: Session, account_id: str) -> None:
    if AccountRepository(session).get(account_id) is None:
        raise DerivedDataError(
            DerivedDataErrorCode.ACCOUNT_NOT_FOUND,
            "derived-data account does not exist",
        )


def _change_type(value: str | None) -> SourceDataChangeType | None:
    return None if value is None else SourceDataChangeType(value)


def _revision_contract(
    account_id: str, record: FinancialDataRevisionRecord | None
) -> FinancialDataRevision:
    if record is None:
        return FinancialDataRevision(
            account_id=account_id,
            revision=0,
            last_change_type=None,
            changed_at=None,
        )
    return FinancialDataRevision(
        account_id=record.account_id,
        revision=record.revision,
        last_change_type=_change_type(record.last_change_type),
        changed_at=record.changed_at,
    )


def _freshness_contract(record: DerivedResultStateRecord) -> DerivedResultFreshness:
    return DerivedResultFreshness(
        account_id=record.account_id,
        output_type=DerivedOutputType(record.output_type),
        status=DerivedResultStatus(record.status),
        required_revision=record.required_revision,
        computed_revision=record.computed_revision,
        generated_at=record.generated_at,
        invalidated_at=record.invalidated_at,
        invalidated_by=(
            None
            if record.invalidated_by is None
            else SourceDataChangeType(record.invalidated_by)
        ),
    )


def _new_unavailable_state(
    *,
    account_id: str,
    output_type: DerivedOutputType,
    required_revision: int,
    invalidated_at: datetime | None,
    invalidated_by: SourceDataChangeType | None,
) -> DerivedResultStateRecord:
    return DerivedResultStateRecord(
        id=new_id(),
        account_id=account_id,
        output_type=output_type.value,
        status=DerivedResultStatus.UNAVAILABLE.value,
        required_revision=required_revision,
        computed_revision=None,
        generated_at=None,
        invalidated_at=invalidated_at,
        invalidated_by=None if invalidated_by is None else invalidated_by.value,
    )


def invalidate_derived_results_in_session(
    session: Session,
    *,
    account_id: str,
    change_type: SourceDataChangeType,
    changed_at: datetime,
) -> DerivedInvalidation:
    """Increment and invalidate atomically inside an existing source-data write."""
    event_time = _aware_utc(changed_at, code=DerivedDataErrorCode.INVALID_CHANGE_TIME)
    _require_account(session, account_id)
    repository = DerivedDataRepository(session)
    revision = repository.get_revision(account_id)
    if (
        revision is not None
        and revision.changed_at is not None
        and event_time < revision.changed_at
    ):
        raise DerivedDataError(
            DerivedDataErrorCode.INVALID_CHANGE_TIME,
            "source-data change cannot predate the latest account revision",
        )
    if revision is None:
        revision = repository.add_revision(
            FinancialDataRevisionRecord(
                account_id=account_id,
                revision=1,
                last_change_type=change_type.value,
                changed_at=event_time,
            )
        )
    else:
        revision.revision += 1
        revision.last_change_type = change_type.value
        revision.changed_at = event_time

    affected = dependent_outputs(change_type)
    states: list[DerivedResultStateRecord] = []
    for output_type in affected:
        state = repository.get_state(account_id, output_type.value)
        if state is None:
            state = repository.add_state(
                _new_unavailable_state(
                    account_id=account_id,
                    output_type=output_type,
                    required_revision=revision.revision,
                    invalidated_at=event_time,
                    invalidated_by=change_type,
                )
            )
        else:
            state.required_revision = revision.revision
            state.status = (
                DerivedResultStatus.UNAVAILABLE.value
                if state.computed_revision is None
                else DerivedResultStatus.STALE.value
            )
            state.invalidated_at = event_time
            state.invalidated_by = change_type.value
        states.append(state)
    session.flush()
    return DerivedInvalidation(
        revision=_revision_contract(account_id, revision),
        affected_outputs=affected,
        freshness=tuple(_freshness_contract(item) for item in states),
    )


def record_source_data_change(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    change_type: SourceDataChangeType,
) -> DerivedInvalidation:
    """Record a server-timestamped change when no owning write service exists yet."""
    with session_scope(factory) as session:
        return invalidate_derived_results_in_session(
            session,
            account_id=account_id,
            change_type=change_type,
            changed_at=utc_now(),
        )


def get_financial_data_revision(
    factory: sessionmaker[Session], *, account_id: str
) -> FinancialDataRevision:
    """Return revision zero before an account has recorded a source change."""
    with session_scope(factory) as session:
        _require_account(session, account_id)
        return _revision_contract(
            account_id, DerivedDataRepository(session).get_revision(account_id)
        )


def list_derived_result_freshness(
    factory: sessionmaker[Session], *, account_id: str
) -> tuple[DerivedResultFreshness, ...]:
    """Return one visible freshness state for every governed output family."""
    with session_scope(factory) as session:
        _require_account(session, account_id)
        repository = DerivedDataRepository(session)
        persisted = {
            DerivedOutputType(item.output_type): item
            for item in repository.list_states(account_id)
        }
        result: list[DerivedResultFreshness] = []
        for output_type in DerivedOutputType:
            state = persisted.get(output_type)
            if state is None:
                result.append(
                    DerivedResultFreshness(
                        account_id=account_id,
                        output_type=output_type,
                        status=DerivedResultStatus.UNAVAILABLE,
                        required_revision=0,
                        computed_revision=None,
                        generated_at=None,
                        invalidated_at=None,
                        invalidated_by=None,
                    )
                )
            else:
                result.append(_freshness_contract(state))
        return tuple(result)


def begin_derived_computation(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    output_type: DerivedOutputType,
) -> DerivedComputationToken:
    """Capture and persist the exact dependency revision before recomputation."""
    started_at = utc_now()
    with session_scope(factory) as session:
        _require_account(session, account_id)
        repository = DerivedDataRepository(session)
        state = repository.get_state(account_id, output_type.value)
        if state is None:
            state = repository.add_state(
                _new_unavailable_state(
                    account_id=account_id,
                    output_type=output_type,
                    required_revision=0,
                    invalidated_at=None,
                    invalidated_by=None,
                )
            )
        return DerivedComputationToken(
            account_id=account_id,
            output_type=output_type,
            required_revision=state.required_revision,
            started_at=started_at,
        )


def complete_derived_computation(
    factory: sessionmaker[Session],
    *,
    token: DerivedComputationToken,
) -> DerivedResultFreshness:
    """Mark current only if no relevant source change occurred during computation."""
    completed_at = utc_now()
    if token.started_at > completed_at:
        raise DerivedDataError(
            DerivedDataErrorCode.INVALID_COMPUTATION_TOKEN,
            "derived computation token cannot start in the future",
        )
    with session_scope(factory) as session:
        _require_account(session, token.account_id)
        repository = DerivedDataRepository(session)
        state = repository.get_state(token.account_id, token.output_type.value)
        if state is None or state.required_revision != token.required_revision:
            raise DerivedDataError(
                DerivedDataErrorCode.SOURCE_CHANGED_DURING_RECOMPUTATION,
                "source data changed before derived recomputation completed",
            )
        state.status = DerivedResultStatus.CURRENT.value
        state.computed_revision = state.required_revision
        state.generated_at = completed_at
        state.invalidated_at = None
        state.invalidated_by = None
        session.flush()
        return _freshness_contract(state)


def recompute_derived_result[PayloadT](
    factory: sessionmaker[Session],
    *,
    account_id: str,
    output_type: DerivedOutputType,
    compute: Callable[[], PayloadT],
) -> DerivedRefreshResult[PayloadT]:
    """Recompute transiently and commit freshness only after a revision-safe result."""
    token = begin_derived_computation(
        factory,
        account_id=account_id,
        output_type=output_type,
    )
    payload = compute()
    freshness = complete_derived_computation(factory, token=token)
    return DerivedRefreshResult(payload=payload, freshness=freshness)


def require_current_derived_result(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    output_type: DerivedOutputType,
) -> DerivedResultFreshness:
    """Fail closed instead of allowing a missing or stale result to be displayed."""
    freshness = next(
        item
        for item in list_derived_result_freshness(factory, account_id=account_id)
        if item.output_type is output_type
    )
    if freshness.status is not DerivedResultStatus.CURRENT:
        raise DerivedDataError(
            DerivedDataErrorCode.RESULT_NOT_CURRENT,
            "derived result must be recomputed before it can be displayed",
        )
    return freshness


__all__ = [
    "DerivedDataError",
    "DerivedDataErrorCode",
    "begin_derived_computation",
    "complete_derived_computation",
    "dependent_outputs",
    "get_financial_data_revision",
    "invalidate_derived_results_in_session",
    "list_derived_result_freshness",
    "recompute_derived_result",
    "record_source_data_change",
    "require_current_derived_result",
]

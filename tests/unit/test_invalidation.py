"""Tests for revision-based derived-data invalidation and recomputation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.invalidation import (
    DerivedDataError,
    DerivedDataErrorCode,
    begin_derived_computation,
    complete_derived_computation,
    complete_derived_computations,
    dependent_outputs,
    get_financial_data_revision,
    list_derived_result_freshness,
    recompute_derived_result,
    record_source_data_change,
    require_current_derived_result,
)
from cashflow_ai.invalidation.demo import main as invalidation_demo_main
from cashflow_ai.invalidation.service import invalidate_derived_results_in_session
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import AccountRecord, UserProfileRecord
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

_TIME = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine: Engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with session_scope(result) as session:
        session.add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id="synthetic-account",
                user_profile_id="synthetic-profile",
                name="Fictional Account",
                account_type="current",
                currency="GBP",
            )
        )
    return result


def _by_output(
    states: tuple[DerivedResultFreshness, ...],
) -> dict[DerivedOutputType, DerivedResultFreshness]:
    return {item.output_type: item for item in states}


def test_dependency_matrix_is_explicit_and_selective() -> None:
    all_outputs = tuple(DerivedOutputType)
    for change_type in (
        SourceDataChangeType.OCR_CORRECTED,
        SourceDataChangeType.TRANSACTION_AMOUNT_CHANGED,
        SourceDataChangeType.FINANCIAL_ROLE_CHANGED,
        SourceDataChangeType.TRANSFER_CONFIRMED,
        SourceDataChangeType.STATEMENT_ADDED,
        SourceDataChangeType.IMPORT_DELETED,
    ):
        assert dependent_outputs(change_type) == all_outputs
    assert dependent_outputs(SourceDataChangeType.CATEGORY_CHANGED) == (
        DerivedOutputType.ANALYTICS,
        DerivedOutputType.ANOMALY_ALERTS,
        DerivedOutputType.BUDGETS,
        DerivedOutputType.SCENARIOS,
        DerivedOutputType.MODEL_PERFORMANCE_COMPARISONS,
    )
    assert dependent_outputs(SourceDataChangeType.CURRENT_BALANCE_CHANGED) == (
        DerivedOutputType.ANALYTICS,
        DerivedOutputType.BUDGETS,
        DerivedOutputType.FORECASTS,
        DerivedOutputType.SCENARIOS,
    )


def test_initial_account_has_revision_zero_and_unavailable_outputs(
    factory: sessionmaker[Session],
) -> None:
    revision = get_financial_data_revision(factory, account_id="synthetic-account")
    states = list_derived_result_freshness(factory, account_id="synthetic-account")

    assert revision == FinancialDataRevision(
        account_id="synthetic-account",
        revision=0,
        last_change_type=None,
        changed_at=None,
    )
    assert tuple(item.output_type for item in states) == tuple(DerivedOutputType)
    assert all(item.status is DerivedResultStatus.UNAVAILABLE for item in states)
    assert all(item.required_revision == 0 for item in states)


def test_first_selective_change_leaves_unaffected_output_at_origin(
    factory: sessionmaker[Session],
) -> None:
    record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.CURRENT_BALANCE_CHANGED,
    )

    states = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )
    recurrence = states[DerivedOutputType.RECURRING_SERIES]
    assert recurrence.status is DerivedResultStatus.UNAVAILABLE
    assert recurrence.required_revision == 0
    assert recurrence.invalidated_at is None
    assert recurrence.invalidated_by is None

    refreshed = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.RECURRING_SERIES,
        compute=lambda: "fictional recurring series",
    )
    assert refreshed.freshness.status is DerivedResultStatus.CURRENT
    assert refreshed.freshness.computed_revision == 0
    assert refreshed.freshness.required_revision == 0


def test_selective_invalidation_marks_current_results_stale_and_increments_revision(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cashflow_ai.invalidation.service.utc_now", lambda: _TIME)
    first = record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.STATEMENT_ADDED,
    )
    assert first.revision.revision == 1
    assert first.affected_outputs == tuple(DerivedOutputType)
    assert all(
        item.status is DerivedResultStatus.UNAVAILABLE for item in first.freshness
    )
    analytics = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        compute=lambda: {"fictional_net_cash_flow": "100.00"},
    )
    forecast = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.FORECASTS,
        compute=lambda: "fictional forecast",
    )
    assert analytics.freshness.status is DerivedResultStatus.CURRENT
    assert analytics.payload == {"fictional_net_cash_flow": "100.00"}
    assert forecast.freshness.status is DerivedResultStatus.CURRENT

    monkeypatch.setattr(
        "cashflow_ai.invalidation.service.utc_now", lambda: _TIME + timedelta(seconds=1)
    )
    second = record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.CATEGORY_CHANGED,
    )
    states = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )

    assert second.revision.revision == 2
    assert states[DerivedOutputType.ANALYTICS].status is DerivedResultStatus.STALE
    assert states[DerivedOutputType.ANALYTICS].computed_revision == 1
    assert states[DerivedOutputType.ANALYTICS].required_revision == 2
    assert states[DerivedOutputType.FORECASTS].status is DerivedResultStatus.CURRENT
    assert states[DerivedOutputType.FORECASTS].required_revision == 1


def test_recomputation_fails_closed_when_relevant_source_changes_mid_run(
    factory: sessionmaker[Session],
) -> None:
    record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.STATEMENT_ADDED,
    )
    recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        compute=lambda: "first fictional result",
    )

    def change_during_compute() -> str:
        record_source_data_change(
            factory,
            account_id="synthetic-account",
            change_type=SourceDataChangeType.TRANSACTION_AMOUNT_CHANGED,
        )
        return "must not become current"

    with pytest.raises(DerivedDataError) as error:
        recompute_derived_result(
            factory,
            account_id="synthetic-account",
            output_type=DerivedOutputType.ANALYTICS,
            compute=change_during_compute,
        )
    assert error.value.code is DerivedDataErrorCode.SOURCE_CHANGED_DURING_RECOMPUTATION
    analytics = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )[DerivedOutputType.ANALYTICS]
    assert analytics.status is DerivedResultStatus.STALE
    assert analytics.computed_revision is not None
    assert analytics.computed_revision < analytics.required_revision


def test_failed_callback_leaves_existing_freshness_unchanged(
    factory: sessionmaker[Session],
) -> None:
    before = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )[DerivedOutputType.BUDGETS]

    def fail() -> None:
        raise RuntimeError("synthetic recomputation failure")

    with pytest.raises(RuntimeError, match="synthetic recomputation failure"):
        recompute_derived_result(
            factory,
            account_id="synthetic-account",
            output_type=DerivedOutputType.BUDGETS,
            compute=fail,
        )
    after = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )[DerivedOutputType.BUDGETS]
    assert after == before


def test_multi_account_completion_is_atomic(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        session.add(
            AccountRecord(
                id="second-synthetic-account",
                user_profile_id="synthetic-profile",
                name="Second Fictional Account",
                account_type="savings",
                currency="GBP",
            )
        )
    first = begin_derived_computation(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
    )
    second = begin_derived_computation(
        factory,
        account_id="second-synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
    )
    record_source_data_change(
        factory,
        account_id="second-synthetic-account",
        change_type=SourceDataChangeType.STATEMENT_ADDED,
    )

    with pytest.raises(DerivedDataError) as changed:
        complete_derived_computations(factory, tokens=(first, second))

    assert (
        changed.value.code is DerivedDataErrorCode.SOURCE_CHANGED_DURING_RECOMPUTATION
    )
    first_state = _by_output(
        list_derived_result_freshness(factory, account_id="synthetic-account")
    )[DerivedOutputType.ANALYTICS]
    assert first_state.status is DerivedResultStatus.UNAVAILABLE
    assert complete_derived_computations(factory, tokens=()) == ()

    with pytest.raises(DerivedDataError) as duplicate:
        complete_derived_computations(factory, tokens=(first, first))
    assert duplicate.value.code is DerivedDataErrorCode.INVALID_COMPUTATION_TOKEN


def test_current_guard_rejects_unavailable_then_accepts_recomputed_result(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(DerivedDataError) as unavailable:
        require_current_derived_result(
            factory,
            account_id="synthetic-account",
            output_type=DerivedOutputType.SCENARIOS,
        )
    assert unavailable.value.code is DerivedDataErrorCode.RESULT_NOT_CURRENT

    refreshed = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.SCENARIOS,
        compute=lambda: ("fictional baseline", "fictional scenario"),
    )
    current = require_current_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.SCENARIOS,
    )
    assert current == refreshed.freshness


def test_invalid_times_tokens_and_missing_accounts_fail_safely(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        session_scope(factory) as session,
        pytest.raises(DerivedDataError) as naive,
    ):
        invalidate_derived_results_in_session(
            session,
            account_id="synthetic-account",
            change_type=SourceDataChangeType.OCR_CORRECTED,
            changed_at=datetime(2026, 9, 1, 12),
        )
    assert naive.value.code is DerivedDataErrorCode.INVALID_CHANGE_TIME

    with session_scope(factory) as session:
        invalidate_derived_results_in_session(
            session,
            account_id="synthetic-account",
            change_type=SourceDataChangeType.OCR_CORRECTED,
            changed_at=_TIME,
        )
    with (
        session_scope(factory) as session,
        pytest.raises(DerivedDataError) as earlier,
    ):
        invalidate_derived_results_in_session(
            session,
            account_id="synthetic-account",
            change_type=SourceDataChangeType.IMPORT_DELETED,
            changed_at=_TIME - timedelta(seconds=1),
        )
    assert earlier.value.code is DerivedDataErrorCode.INVALID_CHANGE_TIME

    monkeypatch.setattr("cashflow_ai.invalidation.service.utc_now", lambda: _TIME)
    future = DerivedComputationToken(
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        required_revision=1,
        started_at=_TIME + timedelta(seconds=1),
    )
    with pytest.raises(DerivedDataError) as token_error:
        complete_derived_computation(factory, token=future)
    assert token_error.value.code is DerivedDataErrorCode.INVALID_COMPUTATION_TOKEN

    missing_state = DerivedComputationToken(
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        required_revision=999,
        started_at=_TIME,
    )
    with pytest.raises(DerivedDataError) as changed:
        complete_derived_computation(factory, token=missing_state)
    assert (
        changed.value.code is DerivedDataErrorCode.SOURCE_CHANGED_DURING_RECOMPUTATION
    )

    for operation in (
        lambda: get_financial_data_revision(factory, account_id="missing-account"),
        lambda: list_derived_result_freshness(factory, account_id="missing-account"),
        lambda: begin_derived_computation(
            factory,
            account_id="missing-account",
            output_type=DerivedOutputType.ANALYTICS,
        ),
        lambda: record_source_data_change(
            factory,
            account_id="missing-account",
            change_type=SourceDataChangeType.CURRENT_BALANCE_CHANGED,
        ),
    ):
        with pytest.raises(DerivedDataError) as missing:
            operation()
        assert missing.value.code is DerivedDataErrorCode.ACCOUNT_NOT_FOUND


def test_invalidation_contracts_reject_inconsistent_metadata() -> None:
    current = {
        "account_id": "synthetic-account",
        "output_type": DerivedOutputType.ANALYTICS,
        "status": DerivedResultStatus.CURRENT,
        "required_revision": 1,
        "computed_revision": 1,
        "generated_at": _TIME,
        "invalidated_at": None,
        "invalidated_by": None,
    }
    invalid_freshness = (
        {**current, "computed_revision": 0},
        {
            **current,
            "status": DerivedResultStatus.STALE,
            "computed_revision": 1,
            "invalidated_at": _TIME,
            "invalidated_by": SourceDataChangeType.STATEMENT_ADDED,
        },
        {
            **current,
            "status": DerivedResultStatus.UNAVAILABLE,
            "computed_revision": None,
            "generated_at": None,
        },
        {
            **current,
            "status": DerivedResultStatus.UNAVAILABLE,
            "required_revision": 0,
            "computed_revision": None,
            "generated_at": None,
            "invalidated_at": _TIME,
            "invalidated_by": None,
        },
        {
            **current,
            "status": DerivedResultStatus.UNAVAILABLE,
            "required_revision": 0,
            "computed_revision": None,
            "generated_at": None,
            "invalidated_at": None,
            "invalidated_by": SourceDataChangeType.STATEMENT_ADDED,
        },
    )
    for values in invalid_freshness:
        with pytest.raises(ValidationError):
            DerivedResultFreshness(**values)  # type: ignore[arg-type]

    for values in (
        {
            "account_id": "synthetic-account",
            "revision": 0,
            "last_change_type": SourceDataChangeType.STATEMENT_ADDED,
            "changed_at": _TIME,
        },
        {
            "account_id": "synthetic-account",
            "revision": 1,
            "last_change_type": None,
            "changed_at": None,
        },
    ):
        with pytest.raises(ValidationError):
            FinancialDataRevision(**values)  # type: ignore[arg-type]

    unavailable = DerivedResultFreshness(
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        status=DerivedResultStatus.UNAVAILABLE,
        required_revision=1,
        computed_revision=None,
        generated_at=None,
        invalidated_at=_TIME,
        invalidated_by=SourceDataChangeType.STATEMENT_ADDED,
    )
    revision = FinancialDataRevision(
        account_id="synthetic-account",
        revision=1,
        last_change_type=SourceDataChangeType.STATEMENT_ADDED,
        changed_at=_TIME,
    )
    with pytest.raises(ValidationError):
        DerivedInvalidation(
            revision=revision,
            affected_outputs=(DerivedOutputType.BUDGETS,),
            freshness=(unavailable,),
        )
    with pytest.raises(ValidationError):
        DerivedInvalidation(
            revision=revision,
            affected_outputs=(DerivedOutputType.ANALYTICS,),
            freshness=(unavailable.model_copy(update={"required_revision": 2}),),
        )
    with pytest.raises(ValidationError):
        DerivedRefreshResult(
            payload="fictional",
            freshness=unavailable,
        )


def test_manual_demo_shows_selective_refresh_and_race_protection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalidation_demo_main()

    output = capsys.readouterr().out
    assert "initial analytics status: unavailable" in output
    assert "statement revision: 1" in output
    assert "analytics after recompute: current" in output
    assert "analytics after category change: stale" in output
    assert "forecast after category change: current" in output
    assert "analytics after refresh: current" in output
    assert (
        "mid-computation change rejected: source_changed_during_recomputation" in output
    )
    assert "derived payload persisted: false" in output

"""Readable synthetic demonstration of derived-result invalidation and refresh."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.invalidation.service import (
    DerivedDataError,
    begin_derived_computation,
    complete_derived_computation,
    list_derived_result_freshness,
    recompute_derived_result,
    record_source_data_change,
    require_current_derived_result,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import AccountRecord, UserProfileRecord
from cashflow_ai.schemas.invalidation import (
    DerivedOutputType,
    DerivedResultFreshness,
    SourceDataChangeType,
)


def _factory() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
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
    return factory


def _status(
    factory: sessionmaker[Session], output_type: DerivedOutputType
) -> DerivedResultFreshness:
    return next(
        item
        for item in list_derived_result_freshness(
            factory, account_id="synthetic-account"
        )
        if item.output_type is output_type
    )


def main() -> None:
    """Print one fictional invalidation, refresh, and race-rejection lifecycle."""
    factory = _factory()
    initial = _status(factory, DerivedOutputType.ANALYTICS)
    statement = record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.STATEMENT_ADDED,
    )
    analytics = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        compute=lambda: {"fictional_net_cash_flow": "100.00"},
    )
    recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.FORECASTS,
        compute=lambda: {"fictional_end_balance": "900.00"},
    )
    category = record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.CATEGORY_CHANGED,
    )
    stale_analytics = _status(factory, DerivedOutputType.ANALYTICS)
    current_forecast = require_current_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.FORECASTS,
    )
    refreshed = recompute_derived_result(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
        compute=lambda: {"fictional_net_cash_flow": "100.00"},
    )
    token = begin_derived_computation(
        factory,
        account_id="synthetic-account",
        output_type=DerivedOutputType.ANALYTICS,
    )
    record_source_data_change(
        factory,
        account_id="synthetic-account",
        change_type=SourceDataChangeType.TRANSACTION_AMOUNT_CHANGED,
    )
    race_code = "none"
    try:
        complete_derived_computation(factory, token=token)
    except DerivedDataError as error:
        race_code = error.code.value

    print("CashFlow AI synthetic derived-data freshness check")
    print(f"initial analytics status: {initial.status.value}")
    print(f"statement revision: {statement.revision.revision}")
    print(f"analytics after recompute: {analytics.freshness.status.value}")
    print(f"category-change revision: {category.revision.revision}")
    print(f"analytics after category change: {stale_analytics.status.value}")
    print(f"forecast after category change: {current_forecast.status.value}")
    print(f"analytics after refresh: {refreshed.freshness.status.value}")
    print(f"mid-computation change rejected: {race_code}")
    print("derived payload persisted: false")


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()

"""Tests for deterministic, privacy-safe transaction categorisation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.categorisation import (
    CategorisationServiceError,
    CategorisationServiceErrorCode,
    categorise_verified_transactions,
)
from cashflow_ai.persistence import (
    Base,
    CategorisationRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    CategoryCorrectionRecord,
    CategoryRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas import (
    AnalyticsScope,
    AnalyticsView,
    CategorisationPlan,
    CategoryDecisionSource,
    CategoryDefinition,
    CategoryExplanationCode,
    CategoryMatchField,
    CategoryRuleSet,
    CategoryTaxonomy,
    DateRange,
    Direction,
    FinancialRole,
    KeywordCategoryRule,
    MerchantCategoryMapping,
    ScopedCategoryRule,
    load_category_rule_set,
    load_taxonomy,
    validate_rule_set_taxonomy,
)
from cashflow_ai.schemas.categorisation import normalise_rule_text

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
TAXONOMY_PATH = Path("configs/categories.yaml")
RULES_PATH = Path("configs/category_rules.yaml")


def _required[T](value: T | None) -> T:
    assert value is not None
    return value


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_foundation(factory: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    with session_scope(factory) as session:
        session.add_all(
            [
                UserProfileRecord(
                    id="profile-1",
                    display_name="Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
                UserProfileRecord(
                    id="profile-2",
                    display_name="Other Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="current-1",
                    user_profile_id="profile-1",
                    name="Synthetic Current",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id="savings-1",
                    user_profile_id="profile-1",
                    name="Synthetic Savings",
                    account_type="savings",
                    currency="GBP",
                ),
                AccountRecord(
                    id="other-1",
                    user_profile_id="profile-2",
                    name="Other Synthetic Current",
                    account_type="current",
                    currency="GBP",
                ),
            ]
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            CategoryRecord(
                id=item.id,
                name=item.name,
                parent_id=item.parent_id,
                taxonomy_version=taxonomy.version,
                is_active=item.is_active,
            )
            for item in taxonomy.categories
        )


def _add_transaction(
    session: Session,
    transaction_id: str,
    *,
    amount: str = "-10.00",
    description: str = "Synthetic purchase",
    merchant: str | None = None,
    account_id: str = "current-1",
    transaction_date: date = date(2026, 8, 5),
    category_id: str | None = None,
    role: FinancialRole = FinancialRole.EXPENSE,
    note: str | None = None,
) -> VerifiedTransactionRecord:
    batch_id = f"batch-{transaction_id}"
    batch = ImportBatchRecord(
        id=batch_id,
        account_id=account_id,
        source_type="csv",
        source_filename=f"{transaction_id}.csv",
        file_hash=_hash(f"file-{transaction_id}"),
        mime_type="text/csv",
        byte_size=100,
        verification_status="verified",
        imported_at=NOW,
    )
    session.add(batch)
    session.add(
        ImportContextRecord(
            id=f"context-{transaction_id}",
            import_batch_id=batch_id,
            flags_json=["synthetic_reference_only"],
            note=note,
            created_at=NOW,
        )
    )
    raw = RawTransactionRecord(
        id=f"raw-{transaction_id}",
        import_batch_id=batch_id,
        source_type="csv",
        source_row_number=2,
        page_number=None,
        page_record_number=None,
        raw_payload={
            "Date": transaction_date.isoformat(),
            "Description": description,
            "Amount": amount,
        },
        original_date_text=transaction_date.isoformat(),
        original_description=description,
        original_amount_text=amount,
        parser_name="synthetic_parser",
        parser_version="1.0.0",
        source_fingerprint=_hash(f"source-{transaction_id}"),
        canonical_fingerprint=_hash(f"canonical-{transaction_id}"),
        issues_json=[],
        review_status="confirmed",
        created_at=NOW,
    )
    session.add(raw)
    parsed_amount = Decimal(amount)
    transaction = VerifiedTransactionRecord(
        id=transaction_id,
        raw_transaction_id=raw.id,
        account_id=account_id,
        transaction_date=transaction_date,
        posting_date=date(2026, 8, 6),
        description=description,
        merchant=merchant,
        amount=parsed_amount,
        balance_after=Decimal("900.00"),
        currency="GBP",
        external_id=f"external-{transaction_id}",
        transaction_type="synthetic",
        direction="inflow" if parsed_amount > 0 else "outflow",
        category_id=category_id,
        financial_role_id=role.value,
        verified_at=NOW,
    )
    session.add(transaction)
    session.flush()
    return transaction


def _rules(
    *,
    merchants: tuple[MerchantCategoryMapping, ...] = (),
    keywords: tuple[KeywordCategoryRule, ...] = (),
    taxonomy_version: str = "1.0",
) -> CategoryRuleSet:
    return CategoryRuleSet(
        version="test-rules-1",
        taxonomy_version=taxonomy_version,
        merchant_mappings=merchants,
        keyword_rules=keywords,
    )


def _merchant_rule(
    *,
    rule_id: str = "merchant_synthetic_shop",
    alias: str = "Synthetic Shop",
    category_id: str = "groceries",
) -> MerchantCategoryMapping:
    return MerchantCategoryMapping(
        rule_id=rule_id,
        aliases=(alias,),
        category_id=category_id,
    )


def _keyword_rule(
    *,
    rule_id: str = "keyword_monthly_rent",
    phrase: str = "monthly rent",
    category_id: str = "housing",
    direction: Direction | None = Direction.OUTFLOW,
    priority: int = 100,
) -> KeywordCategoryRule:
    return KeywordCategoryRule(
        rule_id=rule_id,
        phrase=phrase,
        category_id=category_id,
        direction=direction,
        priority=priority,
    )


def _personal_rule(
    *,
    rule_id: str = "personal_synthetic_shop",
    merchant: str = "Synthetic Shop",
    category_id: str = "shopping",
    profile_id: str = "profile-1",
    direction: Direction | None = None,
    account_id: str | None = None,
    description_contains: str | None = None,
    minimum_amount: str | None = None,
    maximum_amount: str | None = None,
    priority: int = 0,
    is_active: bool = True,
) -> ScopedCategoryRule:
    return ScopedCategoryRule(
        rule_id=rule_id,
        user_profile_id=profile_id,
        category_id=category_id,
        merchant=merchant,
        direction=direction,
        account_id=account_id,
        description_contains=description_contains,
        minimum_amount=Decimal(minimum_amount) if minimum_amount is not None else None,
        maximum_amount=Decimal(maximum_amount) if maximum_amount is not None else None,
        priority=priority,
        is_active=is_active,
    )


def _plan(
    *,
    ids: tuple[str, ...] | None = None,
    rules: tuple[ScopedCategoryRule, ...] = (),
    profile_id: str = "profile-1",
) -> CategorisationPlan:
    return CategorisationPlan(
        user_profile_id=profile_id,
        transaction_ids=ids,
        personal_rules=rules,
    )


def test_repository_rule_configuration_is_valid_and_publicly_explainable() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    rule_set = load_category_rule_set(RULES_PATH, taxonomy)

    assert rule_set.version == "rules-1.0"
    assert rule_set.taxonomy_version == taxonomy.version
    assert len(rule_set.merchant_mappings) == 8
    assert len(rule_set.keyword_rules) == 23
    assert {rule.category_id for rule in rule_set.merchant_mappings} >= {
        "groceries",
        "subscriptions",
        "transport",
    }
    assert all(rule.rule_id for rule in rule_set.keyword_rules)


def test_repository_rules_assign_representative_merchant_and_keyword_categories(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "configured-merchant",
            merchant="Tesco Groceries",
            description="Synthetic card purchase",
        )
        _add_transaction(
            session,
            "configured-keyword",
            merchant=None,
            description="Synthetic monthly rent payment",
        )

    decisions = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("configured-merchant", "configured-keyword")),
        rule_set=load_category_rule_set(RULES_PATH, load_taxonomy(TAXONOMY_PATH)),
    )
    by_id = {decision.transaction_id: decision for decision in decisions}

    assert by_id["configured-merchant"].category_id == "groceries"
    assert (
        by_id["configured-merchant"].explanation.rule_id == "merchant_tesco_groceries"
    )
    assert by_id["configured-keyword"].category_id == "housing"
    assert by_id["configured-keyword"].explanation.rule_id == "keyword_housing_rent"


def test_rule_text_normalisation_is_unicode_safe_without_mutating_source() -> None:
    source = "  ＣＡＦÉ\x00_and---SHOP  "

    assert normalise_rule_text(source) == "café and shop"
    assert source == "  ＣＡＦÉ\x00_and---SHOP  "


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"merchant": "___"}, "merchant must contain searchable text"),
        (
            {"description_contains": "---"},
            "description phrase must contain searchable text",
        ),
        ({"minimum_amount": "-0.01"}, "minimum amount must be non-negative"),
        ({"maximum_amount": "-0.01"}, "maximum amount must be non-negative"),
        (
            {"minimum_amount": "20.00", "maximum_amount": "10.00"},
            "maximum amount must not be below",
        ),
    ],
)
def test_invalid_personal_rule_scopes_are_rejected(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _personal_rule(**kwargs)


def test_invalid_dictionary_and_keyword_text_is_rejected() -> None:
    with pytest.raises(ValidationError, match="aliases must contain searchable text"):
        MerchantCategoryMapping(
            rule_id="bad_alias",
            aliases=("---",),
            category_id="other",
        )
    with pytest.raises(ValidationError, match="aliases must be unique"):
        MerchantCategoryMapping(
            rule_id="duplicate_alias",
            aliases=("Synthetic Shop", "synthetic---shop"),
            category_id="other",
        )
    with pytest.raises(ValidationError, match="phrase must contain searchable text"):
        _keyword_rule(phrase="___")


def test_rule_set_rejects_ambiguous_configuration_identity() -> None:
    with pytest.raises(ValidationError, match="rule IDs must be unique"):
        _rules(
            merchants=(_merchant_rule(rule_id="duplicate"),),
            keywords=(_keyword_rule(rule_id="duplicate"),),
        )
    with pytest.raises(ValidationError, match="aliases cannot appear"):
        _rules(
            merchants=(
                _merchant_rule(rule_id="first_alias", alias="Synthetic Shop"),
                _merchant_rule(rule_id="second_alias", alias="synthetic-shop"),
            )
        )
    with pytest.raises(ValidationError, match="phrases must be unique"):
        _rules(
            keywords=(
                _keyword_rule(rule_id="first_phrase", phrase="Monthly Rent"),
                _keyword_rule(rule_id="second_phrase", phrase="monthly---rent"),
            )
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ids": ()}, "selection cannot be empty"),
        ({"ids": ("one", "one")}, "transaction IDs must be unique"),
        (
            {
                "rules": (
                    _personal_rule(rule_id="same_rule"),
                    _personal_rule(rule_id="same_rule", merchant="Second Shop"),
                )
            },
            "rule IDs must be unique",
        ),
        (
            {"rules": (_personal_rule(profile_id="profile-2"),)},
            "must belong to the selected profile",
        ),
    ],
)
def test_categorisation_plan_rejects_ambiguous_scope(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _plan(**kwargs)


def _taxonomy(
    *categories: CategoryDefinition,
    version: str = "1.0",
) -> CategoryTaxonomy:
    return CategoryTaxonomy(version=version, categories=categories)


def test_rule_set_taxonomy_validation_covers_version_targets_and_fallback() -> None:
    active = CategoryDefinition(id="groceries", name="Groceries")
    needs_review = CategoryDefinition(id="needs_review", name="Needs Review")
    rule_set = _rules(merchants=(_merchant_rule(),))

    assert (
        validate_rule_set_taxonomy(
            rule_set,
            _taxonomy(active, needs_review),
        )
        is rule_set
    )
    with pytest.raises(ValueError, match="versions must match"):
        validate_rule_set_taxonomy(rule_set, _taxonomy(active, version="2.0"))
    with pytest.raises(ValueError, match="unknown category rule target"):
        validate_rule_set_taxonomy(rule_set, _taxonomy(needs_review))
    with pytest.raises(ValueError, match="inactive category rule target"):
        validate_rule_set_taxonomy(
            rule_set,
            _taxonomy(
                CategoryDefinition(
                    id="groceries",
                    name="Groceries",
                    is_active=False,
                ),
                needs_review,
            ),
        )
    with pytest.raises(ValueError, match="active needs_review fallback"):
        validate_rule_set_taxonomy(rule_set, _taxonomy(active))
    with pytest.raises(ValueError, match="active needs_review fallback"):
        validate_rule_set_taxonomy(
            rule_set,
            _taxonomy(
                active,
                CategoryDefinition(
                    id="needs_review",
                    name="Needs Review",
                    is_active=False,
                ),
            ),
        )


def test_precedence_preserves_user_decision_then_personal_merchant_keyword_fallback(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        for transaction_id, merchant, description in (
            ("explicit", "Personal Shop", "Monthly rent"),
            ("personal", "Personal Shop", "Monthly rent"),
            ("merchant", "Synthetic Shop", "Monthly rent"),
            ("keyword", "Unmapped Shop", "Monthly RENT payment"),
            ("fallback", None, "Ordinary synthetic purchase"),
        ):
            _add_transaction(
                session,
                transaction_id,
                merchant=merchant,
                description=description,
                category_id="other" if transaction_id == "explicit" else None,
                note="Reference says salary but must remain inert.",
            )
        session.add(
            CategoryCorrectionRecord(
                id="decision-explicit",
                verified_transaction_id="explicit",
                previous_category_id="other",
                new_category_id="health",
                corrected_at=NOW,
            )
        )

    personal_rule = _personal_rule(merchant="Personal Shop", category_id="shopping")
    rule_set = _rules(
        merchants=(_merchant_rule(),),
        keywords=(_keyword_rule(),),
    )
    results = categorise_verified_transactions(
        factory,
        plan=_plan(
            ids=("explicit", "personal", "merchant", "keyword", "fallback"),
            rules=(personal_rule,),
        ),
        rule_set=rule_set,
    )

    by_id = {result.transaction_id: result for result in results}
    assert by_id["explicit"].category_id == "health"
    assert by_id["explicit"].explanation.source is (
        CategoryDecisionSource.TRANSACTION_DECISION
    )
    assert by_id["explicit"].explanation.code is (
        CategoryExplanationCode.TRANSACTION_DECISION_PRESERVED
    )
    assert by_id["explicit"].explanation.rule_id == "decision-explicit"
    assert by_id["personal"].category_id == "shopping"
    assert by_id["personal"].explanation.source is CategoryDecisionSource.PERSONAL_RULE
    assert by_id["merchant"].category_id == "groceries"
    assert by_id["merchant"].explanation.source is (
        CategoryDecisionSource.MERCHANT_MAPPING
    )
    assert by_id["keyword"].category_id == "housing"
    assert by_id["keyword"].explanation.matched_fields == (
        CategoryMatchField.DIRECTION,
        CategoryMatchField.DESCRIPTION,
    )
    assert by_id["fallback"].category_id == "needs_review"
    assert by_id["fallback"].explanation.code is (
        CategoryExplanationCode.NO_DETERMINISTIC_MATCH
    )
    assert all(result.changed for result in results)

    repeated = categorise_verified_transactions(
        factory,
        plan=_plan(
            ids=("explicit", "personal", "merchant", "keyword", "fallback"),
            rules=(personal_rule,),
        ),
        rule_set=rule_set,
    )
    assert all(not result.changed for result in repeated)

    with session_scope(factory) as session:
        assert (
            _required(session.get(VerifiedTransactionRecord, "explicit")).category_id
            == "health"
        )
        assert _required(
            session.get(VerifiedTransactionRecord, "fallback")
        ).category_id == ("needs_review")


def test_personal_rule_scopes_use_exact_merchant_and_all_selected_constraints(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    transactions = (
        ("merchant-only", "Merchant Only", "Plain purchase", "-9.00", "current-1"),
        ("direction", "Direction Shop", "Plain purchase", "-9.00", "current-1"),
        ("account", "Account Shop", "Plain purchase", "-9.00", "savings-1"),
        ("description", "Description Shop", "Annual CAFÉ pass", "-9.00", "current-1"),
        ("minimum", "Amount Shop", "Plain purchase", "-10.00", "current-1"),
        ("maximum", "Amount Shop", "Plain purchase", "-20.00", "current-1"),
        ("merchant-substring", "Merchant Only Express", "Plain", "-9.00", "current-1"),
        ("wrong-direction", "Direction Shop", "Plain", "5.00", "current-1"),
        ("wrong-account", "Account Shop", "Plain", "-9.00", "current-1"),
        (
            "wrong-description",
            "Description Shop",
            "Decafeteria pass",
            "-9.00",
            "current-1",
        ),
        ("below-min", "Amount Shop", "Plain", "-9.99", "current-1"),
        ("above-max", "Amount Shop", "Plain", "-20.01", "current-1"),
        ("no-merchant", None, "Plain", "-9.00", "current-1"),
        ("inactive", "Inactive Shop", "Plain", "-9.00", "current-1"),
    )
    with session_scope(factory) as session:
        for transaction_id, merchant, description, amount, account_id in transactions:
            _add_transaction(
                session,
                transaction_id,
                merchant=merchant,
                description=description,
                amount=amount,
                account_id=account_id,
            )

    rules = (
        _personal_rule(
            rule_id="merchant_only",
            merchant="merchant---only",
            category_id="shopping",
        ),
        _personal_rule(
            rule_id="merchant_direction",
            merchant="Direction Shop",
            category_id="transport",
            direction=Direction.OUTFLOW,
        ),
        _personal_rule(
            rule_id="merchant_account",
            merchant="Account Shop",
            category_id="savings",
            account_id="savings-1",
        ),
        _personal_rule(
            rule_id="merchant_description",
            merchant="Description Shop",
            category_id="subscriptions",
            description_contains="café pass",
        ),
        _personal_rule(
            rule_id="merchant_amount",
            merchant="Amount Shop",
            category_id="groceries",
            minimum_amount="10.00",
            maximum_amount="20.00",
        ),
        _personal_rule(
            rule_id="inactive_rule",
            merchant="Inactive Shop",
            category_id="health",
            is_active=False,
        ),
    )
    results = categorise_verified_transactions(
        factory,
        plan=_plan(rules=rules),
        rule_set=_rules(),
    )
    categories = {result.transaction_id: result.category_id for result in results}

    assert categories["merchant-only"] == "shopping"
    assert categories["direction"] == "transport"
    assert categories["account"] == "savings"
    assert categories["description"] == "subscriptions"
    assert categories["minimum"] == "groceries"
    assert categories["maximum"] == "groceries"
    assert all(
        categories[transaction_id] == "needs_review"
        for transaction_id in (
            "merchant-substring",
            "wrong-direction",
            "wrong-account",
            "wrong-description",
            "below-min",
            "above-max",
            "no-merchant",
            "inactive",
        )
    )
    assert next(
        result for result in results if result.transaction_id == "account"
    ).explanation.matched_fields == (
        CategoryMatchField.MERCHANT,
        CategoryMatchField.ACCOUNT,
    )
    assert next(
        result for result in results if result.transaction_id == "minimum"
    ).explanation.matched_fields == (
        CategoryMatchField.MERCHANT,
        CategoryMatchField.AMOUNT,
    )


def test_personal_rule_priority_specificity_and_ties_are_order_independent(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        for transaction_id in (
            "ranked",
            "specificity",
            "ambiguous",
            "same-category",
        ):
            _add_transaction(
                session,
                transaction_id,
                merchant="Ranked Shop",
                description="Monthly pass",
            )

    broad_high_priority = _personal_rule(
        rule_id="broad_high_priority",
        merchant="Ranked Shop",
        category_id="travel",
        priority=20,
    )
    specific_low_priority = _personal_rule(
        rule_id="specific_low_priority",
        merchant="Ranked Shop",
        category_id="transport",
        description_contains="monthly pass",
        priority=10,
    )
    first = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("ranked",), rules=(specific_low_priority, broad_high_priority)),
        rule_set=_rules(),
    )[0]
    assert first.category_id == "travel"
    assert first.explanation.rule_id == "broad_high_priority"

    equally_prioritised = (
        _personal_rule(
            rule_id="broad_equal_priority",
            merchant="Ranked Shop",
            category_id="shopping",
            priority=25,
        ),
        _personal_rule(
            rule_id="specific_equal_priority",
            merchant="Ranked Shop",
            category_id="subscriptions",
            description_contains="monthly pass",
            priority=25,
        ),
    )
    specificity = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("specificity",), rules=equally_prioritised),
        rule_set=_rules(),
    )[0]
    assert specificity.category_id == "subscriptions"
    assert specificity.explanation.rule_id == "specific_equal_priority"

    ambiguous_rules = (
        _personal_rule(
            rule_id="ambiguous_a",
            merchant="Ranked Shop",
            category_id="shopping",
            priority=30,
        ),
        _personal_rule(
            rule_id="ambiguous_b",
            merchant="Ranked Shop",
            category_id="health",
            priority=30,
        ),
    )
    ambiguous = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("ambiguous",), rules=ambiguous_rules),
        rule_set=_rules(),
    )[0]
    reversed_ambiguous = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("ambiguous",), rules=tuple(reversed(ambiguous_rules))),
        rule_set=_rules(),
    )[0]
    assert ambiguous.category_id == "needs_review"
    assert reversed_ambiguous.category_id == "needs_review"
    assert ambiguous.explanation.code is (
        CategoryExplanationCode.AMBIGUOUS_PERSONAL_RULES
    )
    assert ambiguous.explanation.rule_id is None

    same_category_rules = (
        _personal_rule(
            rule_id="same_category_b",
            merchant="Ranked Shop",
            category_id="education",
            priority=40,
        ),
        _personal_rule(
            rule_id="same_category_a",
            merchant="Ranked Shop",
            category_id="education",
            priority=40,
        ),
    )
    same = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("same-category",), rules=same_category_rules),
        rule_set=_rules(),
    )[0]
    assert same.category_id == "education"
    assert same.explanation.rule_id == "same_category_a"


def test_merchant_matching_is_exact_and_unicode_normalised(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "exact-unicode",
            merchant="\uff2e\uff25\uff34\uff26\uff2c\uff29\uff38.com",
            description="Synthetic streaming payment",
        )
        _add_transaction(
            session,
            "not-substring",
            merchant="Netflix.com Express",
            description="Synthetic streaming payment",
        )
        _add_transaction(
            session,
            "missing-merchant",
            merchant=None,
            description="Synthetic streaming payment",
        )

    results = categorise_verified_transactions(
        factory,
        plan=_plan(),
        rule_set=_rules(
            merchants=(
                _merchant_rule(
                    rule_id="merchant_streaming",
                    alias="Netflix.com",
                    category_id="subscriptions",
                ),
            )
        ),
    )
    categories = {result.transaction_id: result.category_id for result in results}
    assert categories == {
        "exact-unicode": "subscriptions",
        "missing-merchant": "needs_review",
        "not-substring": "needs_review",
    }


def test_keyword_rules_use_whole_phrases_direction_ranking_and_stable_ties(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "priority",
            description="Coffee shop monthly visit",
        )
        _add_transaction(
            session,
            "direction-specific",
            description="Monthly train ticket",
        )
        _add_transaction(
            session,
            "longest",
            description="Annual cinema ticket",
        )
        _add_transaction(
            session,
            "ambiguous-keyword",
            description="Cafe lunch cafe",
        )
        _add_transaction(
            session,
            "same-keyword-category",
            description="Bus fare then cab fare",
        )
        _add_transaction(
            session,
            "whole-token",
            description="Parental support",
        )
        _add_transaction(
            session,
            "direction-mismatch",
            description="Salary payment",
        )

    rules = (
        _keyword_rule(
            rule_id="priority_coffee_shop",
            phrase="coffee shop",
            category_id="eating_out",
            direction=None,
            priority=10,
        ),
        _keyword_rule(
            rule_id="priority_coffee",
            phrase="coffee",
            category_id="shopping",
            direction=None,
            priority=20,
        ),
        _keyword_rule(
            rule_id="direction_train",
            phrase="train",
            category_id="travel",
            direction=None,
            priority=30,
        ),
        _keyword_rule(
            rule_id="direction_train_ticket",
            phrase="train ticket",
            category_id="transport",
            direction=Direction.OUTFLOW,
            priority=30,
        ),
        _keyword_rule(
            rule_id="longest_cinema",
            phrase="cinema",
            category_id="shopping",
            direction=None,
            priority=40,
        ),
        _keyword_rule(
            rule_id="longest_cinema_ticket",
            phrase="cinema ticket",
            category_id="entertainment",
            direction=None,
            priority=40,
        ),
        _keyword_rule(
            rule_id="ambiguous_cafe_lunch",
            phrase="cafe lunch",
            category_id="eating_out",
            direction=None,
            priority=50,
        ),
        _keyword_rule(
            rule_id="ambiguous_lunch_cafe",
            phrase="lunch cafe",
            category_id="groceries",
            direction=None,
            priority=50,
        ),
        _keyword_rule(
            rule_id="same_z_bus_fare",
            phrase="bus fare",
            category_id="transport",
            direction=None,
            priority=60,
        ),
        _keyword_rule(
            rule_id="same_a_cab_fare",
            phrase="cab fare",
            category_id="transport",
            direction=None,
            priority=60,
        ),
        _keyword_rule(
            rule_id="whole_rent",
            phrase="rent",
            category_id="housing",
            direction=None,
            priority=70,
        ),
        _keyword_rule(
            rule_id="inflow_salary",
            phrase="salary",
            category_id="income",
            direction=Direction.INFLOW,
            priority=80,
        ),
    )
    results = categorise_verified_transactions(
        factory,
        plan=_plan(),
        rule_set=_rules(keywords=rules),
    )
    by_id = {result.transaction_id: result for result in results}

    assert by_id["priority"].category_id == "shopping"
    assert by_id["direction-specific"].category_id == "transport"
    assert by_id["longest"].category_id == "entertainment"
    assert by_id["ambiguous-keyword"].category_id == "needs_review"
    assert by_id["ambiguous-keyword"].explanation.code is (
        CategoryExplanationCode.AMBIGUOUS_KEYWORD_RULES
    )
    assert by_id["same-keyword-category"].category_id == "transport"
    assert by_id["same-keyword-category"].explanation.rule_id == "same_a_cab_fare"
    assert by_id["whole-token"].category_id == "needs_review"
    assert by_id["direction-mismatch"].category_id == "needs_review"

    reversed_result = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("ambiguous-keyword",)),
        rule_set=_rules(keywords=tuple(reversed(rules))),
    )[0]
    assert reversed_result.category_id == "needs_review"
    assert reversed_result.explanation.code is (
        CategoryExplanationCode.AMBIGUOUS_KEYWORD_RULES
    )


def test_latest_transaction_specific_correction_wins_deterministically(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "corrected", merchant="Synthetic Shop")
        session.add_all(
            [
                CategoryCorrectionRecord(
                    id="correction-old",
                    verified_transaction_id="corrected",
                    previous_category_id=None,
                    new_category_id="shopping",
                    corrected_at=NOW - timedelta(days=1),
                ),
                CategoryCorrectionRecord(
                    id="correction-tie-a",
                    verified_transaction_id="corrected",
                    previous_category_id="shopping",
                    new_category_id="health",
                    corrected_at=NOW,
                ),
                CategoryCorrectionRecord(
                    id="correction-tie-z",
                    verified_transaction_id="corrected",
                    previous_category_id="health",
                    new_category_id="education",
                    corrected_at=NOW,
                ),
            ]
        )

    result = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("corrected",)),
        rule_set=_rules(merchants=(_merchant_rule(),)),
    )[0]

    assert result.category_id == "education"
    assert result.explanation.rule_id == "correction-tie-z"


def test_needs_review_is_rerunnable_when_a_later_rule_becomes_available(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "later-rule",
            merchant="Later Shop",
            category_id="needs_review",
        )

    first = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("later-rule",)),
        rule_set=_rules(),
    )[0]
    second = categorise_verified_transactions(
        factory,
        plan=_plan(
            ids=("later-rule",),
            rules=(
                _personal_rule(
                    rule_id="later_personal_rule",
                    merchant="Later Shop",
                    category_id="health",
                ),
            ),
        ),
        rule_set=_rules(),
    )[0]

    assert first.category_id == "needs_review"
    assert not first.changed
    assert second.category_id == "health"
    assert second.changed


def test_scope_ownership_failure_is_privacy_safe_and_changes_nothing(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "owned", merchant="Synthetic Shop")
        _add_transaction(
            session,
            "foreign",
            merchant="Synthetic Shop",
            account_id="other-1",
        )

    with pytest.raises(CategorisationServiceError) as error:
        categorise_verified_transactions(
            factory,
            plan=_plan(ids=("owned", "foreign", "missing")),
            rule_set=_rules(merchants=(_merchant_rule(),)),
        )

    assert (
        error.value.code is CategorisationServiceErrorCode.TRANSACTION_SCOPE_NOT_FOUND
    )
    assert "Synthetic Shop" not in str(error.value)
    assert "foreign" not in str(error.value)
    with session_scope(factory) as session:
        assert (
            _required(session.get(VerifiedTransactionRecord, "owned")).category_id
            is None
        )
        assert (
            _required(session.get(VerifiedTransactionRecord, "foreign")).category_id
            is None
        )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", CategorisationServiceErrorCode.CATEGORY_NOT_FOUND),
        ("inactive", CategorisationServiceErrorCode.CATEGORY_INACTIVE),
        ("wrong-version", CategorisationServiceErrorCode.TAXONOMY_VERSION_MISMATCH),
    ],
)
def test_invalid_persisted_category_targets_fail_atomically(
    factory: sessionmaker[Session],
    mutation: str,
    expected_code: CategorisationServiceErrorCode,
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "target-check", merchant="Synthetic Shop")
        category = session.get(CategoryRecord, "groceries")
        assert category is not None
        if mutation == "missing":
            session.delete(category)
        elif mutation == "inactive":
            category.is_active = False
        else:
            category.taxonomy_version = "2.0"

    with pytest.raises(CategorisationServiceError) as error:
        categorise_verified_transactions(
            factory,
            plan=_plan(ids=("target-check",)),
            rule_set=_rules(merchants=(_merchant_rule(),)),
        )

    assert error.value.code is expected_code
    assert "Synthetic Shop" not in str(error.value)
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "target-check")
            ).category_id
            is None
        )


def test_inactive_historical_user_decision_remains_authoritative(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "historical", merchant="Synthetic Shop")
        historical = session.get(CategoryRecord, "other")
        assert historical is not None
        historical.is_active = False
        session.add(
            CategoryCorrectionRecord(
                id="historical-decision",
                verified_transaction_id="historical",
                previous_category_id=None,
                new_category_id="other",
                corrected_at=NOW,
            )
        )

    result = categorise_verified_transactions(
        factory,
        plan=_plan(ids=("historical",)),
        rule_set=_rules(),
    )[0]
    assert result.category_id == "other"
    assert result.explanation.source is CategoryDecisionSource.TRANSACTION_DECISION


def test_unexpected_assignment_failure_rolls_back_the_entire_run(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "first", merchant="Synthetic Shop")
        _add_transaction(session, "second", merchant="Synthetic Shop")

    original = CategorisationRepository.assign_category
    calls = 0

    def fail_on_second(
        self: CategorisationRepository,
        transaction: VerifiedTransactionRecord,
        category_id: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic assignment failure")
        original(self, transaction, category_id)

    monkeypatch.setattr(CategorisationRepository, "assign_category", fail_on_second)
    with pytest.raises(RuntimeError, match="synthetic assignment failure"):
        categorise_verified_transactions(
            factory,
            plan=_plan(ids=("first", "second")),
            rule_set=_rules(merchants=(_merchant_rule(),)),
        )

    with session_scope(factory) as session:
        assert (
            _required(session.get(VerifiedTransactionRecord, "first")).category_id
            is None
        )
        assert (
            _required(session.get(VerifiedTransactionRecord, "second")).category_id
            is None
        )


def test_category_run_changes_only_category_and_explanations_hide_source_values(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "preservation",
            merchant="Synthetic Private Merchant",
            description="Synthetic private line details",
            amount="-42.25",
            role=FinancialRole.EXPENSE,
            note="Synthetic note asking for income classification",
        )
        original = {
            "raw_transaction_id": transaction.raw_transaction_id,
            "account_id": transaction.account_id,
            "transaction_date": transaction.transaction_date,
            "posting_date": transaction.posting_date,
            "description": transaction.description,
            "merchant": transaction.merchant,
            "amount": transaction.amount,
            "balance_after": transaction.balance_after,
            "currency": transaction.currency,
            "external_id": transaction.external_id,
            "transaction_type": transaction.transaction_type,
            "direction": transaction.direction,
            "financial_role_id": transaction.financial_role_id,
            "verified_at": transaction.verified_at,
        }
        raw_payload = cast(
            dict[str, str],
            _required(
                session.get(RawTransactionRecord, "raw-preservation")
            ).raw_payload,
        ).copy()
        note = session.scalar(
            select(ImportContextRecord.note).where(
                ImportContextRecord.id == "context-preservation"
            )
        )
        flags = session.scalar(
            select(ImportContextRecord.flags_json).where(
                ImportContextRecord.id == "context-preservation"
            )
        )

    result = categorise_verified_transactions(
        factory,
        plan=_plan(
            ids=("preservation",),
            rules=(
                _personal_rule(
                    rule_id="private_match",
                    merchant="Synthetic Private Merchant",
                    category_id="shopping",
                ),
            ),
        ),
        rule_set=_rules(),
    )[0]
    explanation = result.explanation.model_dump_json()

    with session_scope(factory) as session:
        stored = session.get(VerifiedTransactionRecord, "preservation")
        assert stored is not None
        assert stored.category_id == "shopping"
        assert {key: getattr(stored, key) for key in original} == original
        assert (
            _required(session.get(RawTransactionRecord, "raw-preservation")).raw_payload
            == raw_payload
        )
        assert (
            session.scalar(
                select(ImportContextRecord.note).where(
                    ImportContextRecord.id == "context-preservation"
                )
            )
            == note
        )
        assert (
            session.scalar(
                select(ImportContextRecord.flags_json).where(
                    ImportContextRecord.id == "context-preservation"
                )
            )
            == flags
        )

    for private_value in (
        "Synthetic Private Merchant",
        "Synthetic private line details",
        "42.25",
        "current-1",
        "Synthetic note asking for income classification",
    ):
        assert private_value not in explanation


def test_empty_profile_run_and_repository_empty_inputs_are_safe(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    assert (
        categorise_verified_transactions(
            factory,
            plan=_plan(profile_id="profile-2"),
            rule_set=_rules(),
        )
        == ()
    )

    with session_scope(factory) as session:
        repository = CategorisationRepository(session)
        assert repository.latest_category_corrections(()) == {}
        assert repository.list_categories(()) == ()


def test_missing_profile_has_a_controlled_privacy_safe_error(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)

    with pytest.raises(CategorisationServiceError) as error:
        categorise_verified_transactions(
            factory,
            plan=_plan(profile_id="missing-profile"),
            rule_set=_rules(),
        )

    assert error.value.code is CategorisationServiceErrorCode.PROFILE_NOT_FOUND
    assert "missing-profile" not in str(error.value)


def test_inactive_personal_rule_does_not_require_or_apply_its_target(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(session, "ignored-rule", merchant="Synthetic Shop")

    result = categorise_verified_transactions(
        factory,
        plan=_plan(
            ids=("ignored-rule",),
            rules=(
                _personal_rule(
                    rule_id="disabled_rule",
                    category_id="category_not_in_database",
                    is_active=False,
                ),
            ),
        ),
        rule_set=_rules(),
    )[0]

    assert result.category_id == "needs_review"
    assert result.rule_set_version == "test-rules-1"


def _add_full_month_coverage(session: Session) -> None:
    batch = ImportBatchRecord(
        id="coverage-batch",
        account_id="current-1",
        source_type="csv",
        source_filename="synthetic-coverage.csv",
        file_hash=_hash("coverage-batch"),
        mime_type="text/csv",
        byte_size=100,
        verification_status="verified",
        imported_at=NOW,
    )
    context = ImportContextRecord(
        id="coverage-context",
        import_batch_id=batch.id,
        flags_json=[],
        note=None,
        created_at=NOW,
    )
    session.add_all([batch, context])
    session.flush()
    session.add(
        StatementCoverageRecord(
            id="coverage-august",
            import_context_id=context.id,
            statement_start_date=date(2026, 8, 1),
            statement_end_date=date(2026, 8, 31),
            coverage_status="complete",
            missing_periods_json=[],
        )
    )


def test_categorisation_feeds_category_analytics_without_changing_headline_math(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_full_month_coverage(session)
        _add_transaction(
            session,
            "analytics-expense",
            merchant="Synthetic Shop",
            description="Synthetic weekly groceries",
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        _add_transaction(
            session,
            "analytics-income",
            merchant=None,
            description="Synthetic payroll",
            amount="100.00",
            role=FinancialRole.INCOME,
        )

    scope = AnalyticsScope(
        user_profile_id="profile-1",
        account_ids=("current-1",),
        period=DateRange(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
        ),
        view=AnalyticsView.ACCOUNT,
    )
    before = compute_cash_flow_analytics(factory, scope)
    categorise_verified_transactions(
        factory,
        plan=_plan(ids=("analytics-expense",)),
        rule_set=_rules(merchants=(_merchant_rule(),)),
    )
    after = compute_cash_flow_analytics(factory, scope)

    assert before.totals == after.totals
    assert [
        (item.category_id, item.amount) for item in cast(Any, before.category_spending)
    ] == [(None, Decimal("25.00"))]
    assert [
        (item.category_id, item.amount) for item in cast(Any, after.category_spending)
    ] == [("groceries", Decimal("25.00"))]
    with session_scope(factory) as session:
        expense = session.get(VerifiedTransactionRecord, "analytics-expense")
        assert expense is not None
        assert expense.financial_role_id == "expense"

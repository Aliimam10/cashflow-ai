# Deterministic transaction categorisation

CashFlow AI can assign useful spending subjects to verified transactions before
machine learning is introduced. For example, expense-role transactions can be
labelled as groceries, housing, utilities, transport, subscriptions, health, or
travel, allowing the existing analytics service to group them meaningfully.

This is a local Python service boundary. It has no upload screen, API endpoint,
or visual correction workflow. It does not import a statement, infer financial
roles, calculate a forecast, or make a budget.

## Taxonomy and rule configuration

The stable Version 1 taxonomy lives in `configs/categories.yaml`. Versioned
known-merchant mappings and keyword rules live in
`configs/category_rules.yaml`; the rule set declares exactly which taxonomy
version it targets. Every configured category must exist and automatic targets
must be active before a run can change any transaction.

Rules compare temporary normalised text. Normalisation applies Unicode NFKC,
case folding, punctuation replacement, control-character removal, and whitespace
collapse. It does not modify the merchant, description, or raw imported values
stored in the database.

- Known merchants require equality after normalisation; an alias is not a
  substring match.
- Keywords require a complete consecutive normalised phrase in the transaction
  description. A direction restriction, when present, must also match.
- Amount comparisons use fixed-precision `Decimal` magnitude, so an inclusive
  range applies consistently to inflows and outflows without converting money to
  floating point.

Duplicate rule identities, aliases, or keyword phrases after normalisation make
the configuration invalid instead of relying on file order.

## Precedence

The first applicable tier wins:

```text
latest transaction-specific user decision
-> active scoped personal rule
-> exact known-merchant mapping
-> whole-phrase keyword rule
-> needs_review
```

An existing category correction supplies the transaction-specific user decision
and remains authoritative across repeat runs. Commit 19 will develop and
evaluate the ML categoriser; Commit 20 will add its prediction between the
keyword tier and `needs_review`. Commit 18 does not load, train, or call a model.

Priority is explicit for personal and keyword rules. More narrowly scoped
matches and stable rule identities provide deterministic tie-breaking where
applicable. Merchant aliases are unique, so only one merchant mapping can match.
If equally ranked rules still propose different categories, the service returns
`needs_review` rather than allowing input order to decide.

## Scoped personal rules

One personal rule always starts with an exact normalised merchant and may add any
combination of:

- transaction direction;
- account identity;
- a complete normalised description phrase; and
- inclusive minimum and maximum absolute amounts.

Every supplied restriction uses AND semantics: all of them must match the same
verified transaction. Rules are bound to the selected local profile and inactive
rules are ignored. Commit 18 accepts these typed rules for one service run but
does not store them. Personal-rule creation, persistence, editing, deletion, and
the user-facing category correction workflow belong to Commit 20.

## Decisions and explanations

Each transaction produces a typed decision containing its previous category,
selected category, taxonomy and rule-set versions, and whether a database value
changed. Its explanation identifies the precedence source, a stable controlled
reason, the selected rule identity where relevant, and the kinds of fields that
matched.

Explanations deliberately omit the actual merchant, description, amount, and
account values. Ambiguous personal or keyword matches receive a controlled
ambiguity reason. A transaction with no deterministic match receives
`needs_review` with a no-match reason; it is never silently assigned to `other`.

Running the same unchanged rules again is safe: an already-correct category is
reported with `changed = false`. An automatically assigned `needs_review`
category can be replaced when a later deterministic rule matches. A latest
explicit transaction correction cannot be overwritten by an automatic rule.

## Persistence and safety boundary

The service scopes every read to one profile and optionally to an explicit,
unique transaction list. Missing or foreign-owned selections fail as one
controlled operation. Invalid, missing, wrong-version, or inactive automatic
category targets are detected before category writes are staged.

All assignments run in one database unit of work. A failure rolls back the whole
run. The only mutable field is `verified_transactions.category_id`; the service
does not alter:

- preserved raw import payloads or extraction provenance;
- descriptions, merchants, dates, amounts, accounts, or currencies;
- financial roles or transfer links;
- statement notes or structured flags; or
- balance, coverage, and analytics records.

Commit 18 reuses the existing transaction, category, and correction tables. It
adds no database migration, third-party dependency, persisted explanation, API,
or UI. All committed examples and tests use fictional synthetic transactions.

## Current limitations

- Merchant and keyword coverage is intentionally conservative rather than
  exhaustive.
- There is no probabilistic fallback until Commit 19 is implemented and
  evaluated.
- Personal rules are caller-supplied in-memory inputs until Commit 20.
- Category explanations are returned for the current run but are not yet shown
  in a review screen.
- Categorisation improves category breakdowns only; recurrence, forecasting,
  budgeting, anomaly detection, and personalised guidance remain later stages.

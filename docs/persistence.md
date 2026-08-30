# Local persistence

CashFlow AI Version 1 stores application state in a local SQLite database using
SQLAlchemy 2.x. Alembic is the only supported schema migration mechanism.

## Configuration and commands

The safe default is:

```text
sqlite:///data/cashflow.db
```

Set `CASHFLOW_DATABASE_URL` in the ignored local `.env` file to use a different
SQLite location. PostgreSQL and remote database URLs are deliberately rejected
in Version 1.

Apply all migrations:

```bash
make db-upgrade
```

Downgrade to the empty base schema:

```bash
make db-downgrade
```

Downgrading to `base` drops all CashFlow AI tables and therefore destroys local
application data. It is a development/recovery command, not a routine startup
operation.

## Schema boundaries

The initial migration creates 19 application tables covering:

- user, account, import, context, coverage, and balance evidence;
- preserved raw transactions and separately verified transactions;
- categories, financial roles, flags, and correction history;
- recurring series, budgets, savings goals, and scenarios; and
- model metadata, forecast runs, and anomaly alerts.

The migration seeds the repository's Version 1 categories and financial roles.
No personal or synthetic transaction rows are seeded.

Migration `0002` makes canonical fingerprints optional only for quarantined raw
rows, adds structured issue storage to those rows, and distinguishes opening
from closing statement balance snapshots. It does not add tables or seed
financial data.

Migration `0003` additively creates `financial_role_suggestions` and
`financial_role_audits`. It does not rewrite existing transactions or seed
financial data. Deterministic suggestion keys prevent repeated rule runs from
duplicating the same review item. Database checks constrain suggestion kind,
status, confidence, counterpart shape, role coherence, review timestamps, and
audit sources.

## Repository transactions

Repositories stage and flush records but do not commit independently. Callers
use `cashflow_ai.persistence.session_scope` to define a unit of work. A clean exit
commits once; any exception rolls back the entire unit. This prevents partially
stored imports.

SQLite foreign keys are enabled for every application connection. Exact file
hashes, source fingerprints, account names, transaction IDs, and other
domain-specific identities have database uniqueness constraints where required.

The confirmed CSV import is one such unit of work. It records document,
statement context, coverage, balances, preserved rows, and accepted transactions
together; any exception rolls all of them back. Invalid and probable-duplicate
rows remain auditable raw evidence but do not receive a verified transaction.
One server-generated UTC receipt time is shared by all availability-bearing
records in that transaction: `import_batches.imported_at`,
`import_contexts.created_at`, `raw_transactions.created_at`,
`verified_transactions.verified_at`, and `balance_snapshots.recorded_at`.
Caller-reported confirmation time is never used to backdate those fields, and a
future client timestamp fails before any database write.

## Balance observations

`balance_snapshots` stores account-balance evidence independently from
`verified_transactions`. Explicitly confirmed CSV statement context creates
opening and closing observations at its coverage boundaries. An accepted unique
CSV row with a running balance creates an observation dated by posting date when
available, otherwise transaction date. These new running-balance rows are not
backfilled into imports written before this behaviour was introduced.

A manual current balance is also a verified snapshot, with no `import_batch_id`.
It does not create an import, transaction, or statement-coverage record. Money is
stored as `NUMERIC(18, 2)` and returned as `Decimal`; zero and negative balances
remain valid observations.

Manual-entry contracts require a timezone-aware recording time and reject an
`as_of_date` later than that recording date. The service rejects a missing or
inactive account and a currency that does not match the account before writing
anything; these failures use stable `account_not_found`, `account_inactive`, and
`account_currency_mismatch` codes.

The balance repository selects only verified, non-future snapshots. A newer
balance date takes precedence over source type. Same-date ties prefer manual,
statement closing, running balance, and statement opening in that order, then
newer recording time and database identity. Transaction and coverage queries
used by freshness apply the same verified/non-future trust boundary.

## Read-only freshness assessment

The financial-data freshness service combines eligible transactions, balances,
and statement coverage without changing stored state. The caller supplies the
maximum age allowed for transaction, balance, and coverage evidence and the
minimum consecutive known-coverage length. Only verified `complete` and
`overlapping` periods and known segments of verified `gapped` periods count;
`partial` and `unknown` periods prove no continuity.

The returned `archive` or `active_forecasting` mode is an assessment only. It
does not execute a forecast or persist a mode change. Missing evidence stays
unknown, while stable warning codes explain which explicit policy conditions did
not pass. `data_freshness_days` is the age of the newest trusted transaction or
balance observation, with separate ages retained for both evidence types.

PDF review remains in memory. No balance from an approved PDF is persisted until
a later service can store its import batch, raw and approved rows, rejected-row
evidence, confirmed coverage, and balance snapshots as one auditable unit.

## Financial-role decisions

System suggestions and user decisions use the same transaction-scoped session
pattern as imports. Generating a suggestion leaves `financial_role_id`
unchanged. Confirmation updates the verified transaction and inserts immutable
audit history together; paired transfer confirmation changes both legs or
neither. Rejection changes only suggestion status.

Direct user actions are also audited when they change a role. `needs_review`
uses the existing structured `user_flags` table and is idempotent. A decision
rejects other pending suggestions involving the affected transaction, while raw
transactions, categories, statement notes, and import context remain unchanged.

Suggestion review times, role-audit change times, and structured review-flag creation
times come from one server UTC receipt time for the operation, not the timestamp
reported by its caller. The caller value is timezone-validated but is not stored
because the schema has no distinct reported-time field. Receipt chronology is
checked against verified transaction evidence, suggestion creation, and prior role
audits before anything is changed.

## Deterministic category assignment

Categorisation reuses the seeded `categories`, `verified_transactions`, and
`category_corrections` tables, so Commit 18 adds no migration or stored rule
table. The latest correction for a transaction is authoritative and prevents a
later automatic run from overwriting that explicit user decision.

The categorisation repository reads verified transactions only through the
selected local profile and optional transaction IDs. Before staging any change,
the service validates that the complete selection is owned, every target exists
in the declared taxonomy version, and every automatic target is active. It then
changes only `verified_transactions.category_id`; raw rows, descriptions,
merchants, amounts, dates, accounts, financial roles, statement notes, and flags
are untouched. All assignments share one session and therefore commit together
or roll back together.

Merchant and keyword definitions are versioned repository configuration rather
than database rows. Scoped personal rules are validated inputs to the current
run and are not persisted by this stage. Commit 20 owns personal-rule storage,
category correction creation, and the corresponding user-review workflow.
Returned category explanations are also not stored as a new report or audit
table.

## Read-only financial analytics

The analytics repository reads a bounded date range and never stages or commits a
record. Accepted rows come directly from `verified_transactions`; a whole import
batch may still need review because a different source row was rejected or marked
as a probable duplicate. Only a linked verified transaction is eligible for
totals. Raw-only, rejected, and probable-duplicate records remain excluded.

Coverage has a stricter trust rule: it contributes only when its import batch is
verified. Queries select every coverage interval that overlaps the requested
range, and the service clips and combines those intervals without treating absent
transactions as evidence of zero activity. Balance queries select only verified
snapshots and apply deterministic same-day source priority before chart segments
are built.

Confirmed paired-transfer suggestions are read only to decide whether movement is
internal to a consolidated account set. Their stored status is not sufficient:
both current transaction roles, opposite amounts, distinct accounts, and currency
must still agree. A later user override therefore makes a stale pair link inert.
Pending suggestions and role-audit history never change current calculations.

Analytics reports are calculated in memory with `Decimal` and are not database
tables. Commit 17 adds no migration, stored aggregate, cache, or new dependency.

## Commit 20 categorisation records

`category_decisions` stores privacy-safe decision provenance, review state,
confidence, and model version. `personal_category_rules` stores only explicitly
requested local rules with merchant and optional direction, account, description,
amount, and priority scope. Migration `0004` is additive; it does not rebuild or
rewrite imported transactions. A later applied resolution marks older pending
decisions as superseded. Category corrections are append-only and use server receipt
time as their historical-visibility timestamp; an explicitly requested personal
rule is stored only after every supplied scope matches its source transaction.

## Commit 21 recurrence records

Migration `0005` adds `recurring_payment_candidates` and
`recurring_payment_members`. Membership preserves verified evidence. Pending,
confirmed, and cancelled review state is stored with its review time; confirmation
links to the existing `recurring_series` table. No source or verified transaction is
rewritten. Candidate review time and confirmed-series creation time share one server
UTC receipt value. Caller-reported review time remains unpersisted request metadata;
future reported values and a server receipt that precedes the candidate's persisted
evidence chronology fail atomically.

Migration `0006` adds the candidate's currency, direction, financial role,
`evidence_as_of_date`, and `knowledge_cutoff_at`, plus `identified_at` on every
member. The migration is additive and deliberately does not merge legacy candidates,
rewrite reviews, or add a uniqueness rule that old `0005` data might violate.

For legacy rows, currency and direction come from the earliest linked verified
transaction when possible; otherwise the conservative compatibility fallbacks are
GBP and the sign of the expected amount. Financial role comes from the newest role
audit no later than the original detection time and falls back to `unknown` rather
than copying a later current role. Evidence date is the newest linked transaction
date capped at detection.

Revision `0005` did not record when the derived candidate identity or individual
membership links became known, so revision `0006` must not guess those historical
times. It writes one migration-execution timestamp to `knowledge_cutoff_at` and every
legacy member's `identified_at`. This conservatively quarantines migrated recurrence
evidence from every forecast cutoff before the migration while preserving the
original detection time, review time, candidate, membership links, and source
transactions unchanged. Recurrence created after the migration stores its actual
service cutoff instead. Services treat a migrated stored review state as pending
before this boundary and use the boundary as the earliest effective confirmation
time afterward. This preserves the original audit values without allowing them to
backdate historical detection or forecasting. A downgrade removes only the new
provenance fields and constraints; reapplying `0006` establishes a new conservative
availability boundary because the discarded provenance cannot be reconstructed.

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

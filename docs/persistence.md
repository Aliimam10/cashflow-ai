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

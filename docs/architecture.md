# Architecture

CashFlow AI will use a local-first modular-monolith architecture. This document
will describe component boundaries, dependency direction, deployment topology,
and important architectural decisions as those components are implemented.

The approved high-level flow is:

```text
CSV ----------------------> CSV parser -----------+
digital bank PDF ---------> text/table extractor +--> review and confirmation
scanned or camera PDF ----> OCR extractor --------+             |
synthetic demo data ------> generated records ------------------+
                                                               v
                                            canonical validation and cleaning
                                                               |
                                                               v
                                                relational storage -> analytics
                                                               |
                                                               v
                                                     FastAPI -> Streamlit
```

Business logic will remain outside API routes and Streamlit pages.

## Statement-source adapters

Source adapters are responsible only for turning an uploaded document into
provisional transaction candidates plus provenance, confidence, warnings, and
the preserved raw source representation.

- The CSV preview adapter accepts in-memory bytes, enforces a 10 MiB default
  limit, detects supported text encodings and delimiters, validates every row's
  shape, and retains only the first 25 rows in its returned preview. It writes
  neither the upload nor accepted transactions to storage.
- CSV mapping contracts keep account selection, statement context, and the
  user's heading choices explicit. They support either one signed-amount column
  or a pair of debit and credit columns, plus optional posting date, running
  balance, currency, external ID, and transaction-type columns.
- Digital-PDF adapters prefer embedded text and table structure from statements
  downloaded from an online banking application.
- OCR adapters handle scanned or camera-captured PDF pages and retain page,
  region, and recognition-confidence metadata.

Adapters do not categorise transactions, calculate analytics, or write accepted
transactions directly to the database. All sources pass through the same
canonical validation rules after the user reviews the extraction preview.

The CSV adapter now passes a fully validated in-memory document to the confirmed
import service. Confirmation is bound to the previewed byte hash. The service,
not the adapter, owns normalisation, duplicate decisions, coverage analysis, and
database orchestration.

PDF extraction is a two-stage decision: attempt digital extraction first, then
fall back to OCR only for pages without usable embedded text. Low-confidence,
ambiguous, or unreconciled rows are highlighted. No PDF-derived transaction is
accepted until the user explicitly confirms the preview.

## Normalisation and duplicate detection

The normaliser is source-independent: CSV mapping currently creates its input,
and later digital-PDF and OCR adapters will create the same preserved-value
contract. It converts supported UK/ISO dates and common bank amount formats,
cleans Unicode and whitespace, removes conservative bank-description wrappers,
derives a merchant and calendar fields, and emits a complete provisional draft.
The exact source values and versioned parser identity remain attached.

Duplicate detection consumes normalised records without mutating them. Exact
source-row fingerprints and matching bank external IDs are high-confidence
duplicates eligible for automatic skipping. Canonical matches, similar
descriptions, equal amounts, and dates up to two days apart contribute to a
probable-match score; probable matches always require review. Repeated-file and
statement-date overlap checks are separate because overlapping statements can
contain both duplicates and legitimate transactions.

## Local persistence

SQLAlchemy 2.x maps explicit persistence records to SQLite tables. Alembic owns
schema creation and upgrades; application startup must not silently call
`create_all`. SQLite foreign-key enforcement is enabled on every application
connection.

Persistence keeps these boundaries explicit:

- raw transactions retain source payload, original text, parser identity, and
  fingerprints;
- verified transactions reference one raw row and contain calculation-ready
  fixed-precision values;
- categories and financial roles use independent foreign keys;
- balance snapshots never share the verified-transaction table;
- import context and statement coverage retain their own one-to-one records;
  and
- model metadata is independent of forecast runs and anomaly alerts so model
  provenance can be reused and audited.

Repositories accept a transaction-scoped SQLAlchemy session and flush changes
without committing. `session_scope` owns the unit of work: it commits once on
success and rolls back every staged change on failure. The confirmed CSV import
composes these repositories as one unit, so a failed row write cannot leave a
batch, context, balance, or partial transaction set behind.

Rejected and probable-duplicate CSV rows remain in `raw_transactions` with
structured issues for audit. Only confirmed unique rows receive a linked
`verified_transactions` record and become eligible for later calculations.

## Configuration

Application configuration is loaded explicitly through
`cashflow_ai.config.load_settings`; importing the package does not read the
environment or filesystem. Development, test, and production profiles provide
safe defaults, while `CASHFLOW_` environment variables and optional `.env`
files provide local overrides. Invalid values fail during settings construction.

The default profile files are `.env` followed by `.env.<environment>`, with the
environment-specific file taking precedence. Process environment variables
override both files.

## Logging

`cashflow_ai.logging.configure_logging` owns process-level logging setup. Local
profiles use readable console logs and production uses one-line JSON records.
Calling the function repeatedly replaces the root handler instead of duplicating
output.

Structured records expose only the standard timestamp, severity, logger, and
message fields plus explicitly supplied `event` and `context` values. Arbitrary
record attributes are not copied into JSON, reducing the risk of leaking private
transaction data.

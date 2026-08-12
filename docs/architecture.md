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
- The digital-PDF adapter validates in-memory documents with PyMuPDF, requires
  usable embedded text on every page, extracts recognised tables with
  pdfplumber, and uses a conservative text fallback for supported layouts.
  It returns review-only candidates and never writes PDF-derived rows directly
  to persistence.
- The OCR adapter renders scanned or camera-captured PDF pages in memory,
  corrects detected orientation, preprocesses them with Pillow, and invokes
  local Tesseract through pytesseract. It retains raw recognised lines and
  page, line, field, and candidate confidence metadata.

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

The digital extractor reports the exact pages that require OCR. The OCR adapter
can process an image-only document independently and returns the same canonical
transaction shape with OCR provenance. It currently OCRs every page supplied to
it; page-by-page combination of digital and OCR results belongs to the shared
review workflow rather than silently mixing evidence inside either adapter.

Rendered images, grayscale copies, and thresholded copies remain in process
memory and are closed after each page. The application does not retain page
image files or OCR artefacts after the preview call.

## Statement reconciliation and review

The shared PDF review service consumes either a digital-PDF or OCR preview. It
does not persist rows. It binds the review to the exact SHA-256 document hash,
source adapter, and source identities. It preserves every original extracted
value and keeps an editable working draft separate from that evidence. Raw
opening and closing balance evidence separately retains the amount text, page,
line, extraction method and parser provenance, and OCR confidence when present.

The service calculates `opening balance + signed transaction total` and compares
the result with the reported closing balance using a one-penny default tolerance.
If either balance or any transaction amount is unavailable, reconciliation is
reported as unavailable rather than inferred from partial data. A difference
outside tolerance is explicit and must be acknowledged before approval.
Explicitly confirmed or corrected balance values and their raw evidence remain
in the approved result even when reconciliation is unavailable. The review also
retains an extracted statement period and requires a confirmed or corrected
period whenever balance evidence exists. Approved transactions must fall inside
that confirmed coverage and outside its explicit gaps, leaving Commit 15 an
authoritative start/end date for statement balance snapshots.

Only extraction errors and OCR fields below the configured confidence threshold
enter the targeted row queue. Ambiguous slash dates and separate debit/credit
columns require statement-level confirmation. The selected date format reparses
ambiguous raw transaction and posting dates unless a user correction already
replaced that field. Corrections may change editable transaction values, but may
not change account, currency, category, or financial role.

Statement approval is bound to the preview hash; all uncertain rows must be
confirmed with complete canonical values or rejected. Approved rows retain their
source identity, fingerprint, original values, extracted draft, provenance,
issues, confidence, and OCR line references. Rejected rows retain the complete
unchanged review row, not only a fingerprint. The boundary currently returns
these in-memory contracts and provides no persistence or UI. Unapproved OCR rows
have no route to trusted analytics, model training, recurrence, anomaly
detection, or forecasting.

## Normalisation and duplicate detection

The normaliser is source-independent: CSV mapping plus the digital-PDF and OCR
adapters create the same preserved-value contract. It converts supported UK/ISO
dates and common bank amount formats,
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

## Balance evidence and financial-data readiness

Balances are observations, not cash-flow events. They remain in the dedicated
balance-snapshot table and never create a verified transaction. The current
writers are deliberately narrow:

- explicitly confirmed CSV statement context creates opening and closing
  snapshots at the confirmed coverage start and end;
- each accepted unique CSV row with a running balance creates a snapshot dated
  by its posting date when present, otherwise by its transaction date; and
- manual current-balance entry creates one verified snapshot without fabricating
  an import batch, statement coverage, or transaction.

Exact duplicates, probable duplicates, rejected rows, and rows without a balance
do not create running-balance evidence. Older imports are not retroactively
backfilled; their retained transaction `balance_after` values remain available
for a future explicit migration or re-import decision.

Balance selection is deterministic. A newer `as_of_date` always wins. For
observations on the same date, source priority is manual, statement closing,
running balance, then statement opening; recording time and database identity
break any remaining tie. Money remains fixed-precision `Decimal`, and future or
unverified observations are ineligible. A zero or negative account balance is
valid evidence rather than a missing value.

The freshness service reads verified transactions, verified balances, and
verified statement coverage for one account at an explicit assessment date. The
caller supplies maximum transaction, balance, and coverage ages plus a minimum
consecutive-coverage length. Only `complete` and `overlapping` coverage, plus the
known segments left after removing every explicit gap from `gapped` coverage,
can prove continuity. `partial` and `unknown` periods cannot. Missing dates stay
unknown and never become zero activity.

The result reports each evidence date and age, the latest qualifying consecutive
coverage, stable warnings, and `data_freshness_days`: the age of the most recent
trusted transaction-or-balance observation. It selects either
`active_forecasting` or `archive`. This mode is a conservative readiness gate
for a later forecasting service, not a forecast, model, API endpoint, or UI
state transition.

Approved PDF balances remain in the in-memory review contract. There is no PDF
database persistence yet, so the system must not write an opening or closing PDF
snapshot in isolation from its approved rows, rejected-row evidence, import
batch, and confirmed coverage. That complete atomic persistence workflow belongs
to a later stage.

## Financial-role interpretation and review

Financial role is the calculation meaning of a verified transaction and remains
separate from category. The role service reads only verified transactions whose
current role is `unknown`. It can persist advisory transfer, refund, and
reimbursement suggestions, but it cannot silently change a transaction.

Transfer matching is restricted to distinct accounts owned by the same local
profile, the same currency, exact opposite `Decimal` amounts, and dates no more
than three days apart. Description similarity, explicit transfer language, and
mentions of the other account strengthen confidence. A transfer-looking row
without a counterpart remains a one-sided review suggestion. Refund and
reimbursement suggestions require a positive amount and explicit controlled
language; a generic positive payment remains `unknown`.

Only an explicit user confirmation or override changes a role. A paired transfer
updates both legs and writes both audit entries inside one database transaction.
Any failure rolls back the entire decision. Role signs are checked, competing
pending suggestions are rejected, categories and raw evidence are untouched,
and repeated suggestion generation is idempotent.

Statement flags and notes are joined into the local review projection for
reference only. They are not inputs to suggestion rules and cannot change roles,
categories, analytics, or forecasts. This module adds no financial totals,
categorisation, API, or interface; the analytics module consumes only current
confirmed roles.

## Coverage-aware analytics

`cashflow_ai.analytics` is a read-only application/domain boundary over narrow
repository queries. It receives an owned account set, an inclusive date range,
and either an account or consolidated view. The repository reads accepted
verified transactions, verified-import coverage, verified balance snapshots,
optional category names, and currently valid confirmed transfer links. It never
loads raw payloads, statement notes, flags, or pending suggestions.

The service performs `Decimal` aggregation in memory and returns typed immutable
contracts. Financial role, rather than category metadata, decides whether a row
is income, expense, refund, reimbursement, cash withdrawal, transfer, excluded,
or unresolved. This keeps Commit 18 category rules from changing headline cash
flow semantics.

Coverage is an independent input to every period result. Complete and overlapping
statement ranges prove their full dates; gapped ranges prove only dates outside
their explicit gaps; partial, unknown, and unverified ranges prove nothing. For
multiple accounts, complete dates are the intersection, partially covered dates
are the union minus that intersection, and dates outside the union remain
missing. No transaction absence is used to infer coverage.

Balance history uses only verified snapshots and collapses same-day evidence with
the existing manual, closing, running, opening priority. Points inside one known
coverage interval may share a chart segment. Every explicit gap starts a new
segment, and a point outside known coverage is standalone, so a later chart
cannot silently bridge missing months.

No analytics output is persisted. The module adds no migration, category rule,
recurrence detector, forecast, anomaly model, route, or visual component.

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

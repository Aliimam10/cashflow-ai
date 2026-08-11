# Data contracts

The schemas under `cashflow_ai.schemas` are the validated boundaries between
statement-source adapters, cleaning, persistence, APIs, and later ML pipelines.
Models reject unknown fields so accidental source data cannot silently cross a
boundary.

## Canonical transaction

Required fields:

- `transaction_date`: ISO 8601 date;
- `description`: non-empty source description;
- `amount`: non-zero signed decimal with at most two fractional digits;
- `direction`: `inflow` or `outflow`, consistent with the amount sign;
- `currency`: GBP in Version 1;
- `account_id`: application account identifier.

Optional fields:

- `posting_date`;
- `merchant`;
- `balance_after`;
- `external_id`;
- `transaction_type`;
- `category_id`.

Income and refunds are positive. Expenses and outgoing transfers are negative.
Python and storage boundaries use `Decimal`; JSON serializes money as decimal
strings. Floating-point money input is rejected rather than silently rounded.
Dates have no implicit timezone. Timestamps use timezone-aware ISO 8601 values.

Missing transaction date, description, amount, direction, currency, or account
rejects a canonical transaction. A missing balance, merchant, external ID,
transaction type, or category remains valid. Provisional extraction drafts may
be incomplete while awaiting correction.

## Import documents and candidates

`ImportDocument` records document identity, batch identity, source type, safe
filename, SHA-256 hash, detected MIME type, byte size, and an aware upload time.
The supported Version 1 source types are:

- CSV;
- digital PDF;
- OCR-derived PDF.

`ImportCandidate` preserves the raw source payload alongside a provisional
transaction, provenance, confidence, issues, and review state. CSV candidates
require a source row number. PDF candidates require a page number. OCR
provenance additionally requires recognition confidence.

Extraction methods must match their source: CSV row parsing, PDF embedded text,
PDF table extraction, or OCR. PDF regions use non-negative page coordinates and
positive dimensions.

Candidates move through `pending`, `needs_review`, `confirmed`, or `rejected`.
The `confirmed` state is invalid unless explicit user confirmation is recorded;
confirmation cannot be attached to any other state.

## Confidence and issues

Confidence values are bounded from 0 to 1. At most one confidence record is
allowed per transaction field. Confidence does not make a value correct; PDF
and OCR rows still require user review.

Issues contain a stable code, human-readable message, severity, and optional
field reference. Later adapters will use them for missing values, ambiguous
signs, low OCR confidence, balance mismatches, and unsupported layouts.

## Category taxonomy

`configs/categories.yaml` is taxonomy version `1.0`. It contains the stable
Version 1 top-level categories, including `other` and `needs_review`. Category
IDs and names must be unique; parent references must exist and cannot contain
self-references or cycles.

The demo-data schema remains a generator-specific labelled format. Source
adapters convert generated or uploaded rows into the same canonical contracts;
the demo schema is not a second application ingestion boundary.

## Accounts and financial roles

Version 1 supports current, checking, and savings accounts. Credit-card
accounts are intentionally excluded. Currency belongs to the account; Version 1
currently validates GBP. Later consolidated analytics may combine accounts only
when their currencies match.

Category describes what a transaction concerns. `FinancialRole` independently
describes how it affects analytics: income, expense, transfer in, transfer out,
refund, reimbursement, cash withdrawal, excluded, or unknown. Canonical
transactions default to `unknown` until later rules or an explicit user decision
assign a role. Commit 6 does not infer roles.

## Source lineage and verification

Extraction provenance can include a validated parser name and version alongside
the existing source type, extraction method, page, region, and confidence.
Documents use `unverified`, `needs_review`, `verified`, or `rejected` status.
These fields allow a later import to be reproduced and prevent unverified OCR
data from silently entering trusted calculations.

## Statement coverage

`StatementCoverage` records statement start and end dates, coverage status, and
explicit missing periods. A complete statement cannot contain missing periods;
a gapped statement must identify at least one. Gaps must be chronological,
non-overlapping, and contained inside the overall statement period.

Missing coverage means unknown financial activity. It must never be filled with
zero-valued spending or income.

## Statement balances and snapshots

`StatementBalances` holds optional opening and closing statement balances and
requires at least one value. It represents reported or explicitly confirmed
values rather than a transaction. `StatementReconciliation` separately records
whether those values reconcile with the extracted signed transaction total.

`BalanceSnapshot` records an account balance at a date and time, its source, and
verification status. Statement and running-balance snapshots retain their source
document ID; manually entered snapshots must not claim a source document. A
snapshot is not a transaction and never appears in transaction totals.

## Statement context

`ImportContext` links an account, coverage, optional balances, structured flags,
and an optional free-text note. Flags record explicit facts such as known
transfers, refunds, reimbursements, cash withdrawals, unusual one-off expenses,
possible missing dates/pages, or historical archive status.

Free-text notes are inert reference metadata. They cannot assign a category or
financial role and cannot directly change analytics, anomalies, or forecasts.

## CSV preview and mapping

`CsvPreview` is a non-persistent structural view of one uploaded CSV. It records
the safe basename, byte size, SHA-256 byte hash, detected encoding and delimiter, unique source
headings, total data-row count, truncation state, and up to the configured
number of preview rows. Preview rows retain their logical source-row number and
unmodified string values; dates and money are deliberately not cleaned yet.

`CsvColumnSuggestions` reports exact matches for common bank-export headings.
Suggestions are advisory: the user or later UI still selects the mapping.
`CsvColumnMapping` requires a transaction date, description, and exactly one of
these amount layouts:

- one signed-amount column; or
- separate debit and credit columns.

Optional selections cover posting date, running balance, currency, external ID,
and transaction type. A source column cannot be assigned two meanings, and
selected columns must exist in the preview. `CsvImportPlan` links the mapping to
an account and the Commit 6 statement context; both must name the same account.

`CsvImportConfirmation` records explicit approval, an aware confirmation time,
and the exact preview hash. `CsvDocument` contains every structurally validated
row for the subsequent in-memory import. `CsvImportSummary` requires its new,
exact-duplicate, probable-duplicate, and rejected counts to account for every
source row; the corresponding review row locations must match those counts.
Its coverage result separates prior gaps from newly exposed gaps and reports
overlap and disconnected date ranges.

## Normalised transactions and fingerprints

`OriginalTransactionValues` retains the original date, description, amount,
debit/credit, balance, currency, identifier, type, and every raw heading/value
pair without trimming the cell strings. `NormalisedTransaction` keeps that
source evidence beside:

- a cleaned, signed, fixed-precision `TransactionDraft`;
- derived year, month, day, weekday, ISO week, and weekend fields;
- the versioned parser identity;
- an exact CSV-row or PDF-page/record identity;
- an immutable source fingerprint; and
- a canonical matching fingerprint.

Both fingerprints are SHA-256 digests. The source fingerprint includes the
document hash, exact location, and original values, so reprocessing does not
change source identity. The canonical fingerprint uses cleaned account, date,
amount, currency, and merchant/description values so different source formats
can be compared. A canonical match is evidence for review, not proof that one
transaction should be deleted.

## Text-PDF extraction previews

`TextPdfPreview` represents one non-persistent, embedded-text PDF extraction.
It records the safe filename, byte size and SHA-256 hash, complete ordered page
previews, extraction layouts, optional statement coverage and balances,
document-level issues, and one or more transaction candidates. The contract
always requires later user confirmation.

`PdfPageExtraction` preserves each page's embedded text, alphanumeric character
count, and number of tables detected. `PdfTransactionCandidate` retains the
original extracted cells, provisional canonical draft, source and canonical
fingerprints, page/record identity, extraction method, parser version, and
structured issues. Its source identity and provenance must both identify the
same digital-PDF page. A candidate without a canonical fingerprint must explain
the failure through at least one issue, and all candidates remain in
`needs_review` state.

The extraction layout is either `table` or `generic_text`. The latter is a
review warning, not evidence that the layout was interpreted perfectly.

## Scanned-PDF OCR previews

`OcrPdfPreview` is the non-persistent result of local scanned-statement OCR. It
binds the preview to the exact PDF hash, contains every ordered page, contains
one or more review-only transaction candidates, and states that no temporary
OCR artefacts were retained.

Each `OcrPageExtraction` records rendered dimensions and DPI, applied rotation,
optional orientation confidence, whether thresholding was used, aggregate page
confidence, raw OCR text, and ordered `OcrLineExtraction` records. Raw page text
must exactly equal those lines joined in order. Line confidence is the mean of
the recognised word confidences and remains advisory.

`OcrTransactionCandidate` preserves the original recognised values separately
from its provisional canonical draft. It records PDF page/record identity,
contributing OCR line numbers, source and canonical fingerprints, field
confidence, parser identity, structured issues, and OCR provenance. Its page
and line references must exist in the preview, and every candidate remains
`needs_review` even when it normalises successfully.

## Statement reconciliation and review

`StatementReview` is a non-persistent approval boundary for an exact digital or
OCR PDF hash and source adapter. Every transaction and balance source identity
must carry that same hash and source type. Each `StatementReviewRow` holds
immutable original extraction values, the extracted draft, a separate editable
working draft, provenance, confidence, issues, and stable targeted-review
reasons. Editing never overwrites the original evidence.

Each `StatementBalanceEvidence` retains whether the raw value was an opening or
closing balance, its unmodified amount text, parsed amount or structured issue,
exact document identity, page and line, extraction method and parser provenance,
and OCR confidence where applicable. Parsed statement balances cannot enter a
review without matching raw PDF evidence.

`StatementReconciliation` records the opening balance, signed transaction total,
expected closing balance, reported closing balance, unexplained difference,
tolerance, and number of unusable amounts. Its status is `reconciled`, `mismatch`,
or `unavailable`; unavailable results cannot claim partial balance arithmetic.

`StatementApproval` must match the exact preview hash and contain an aware audit
timestamp. Ambiguous dates and debit/credit layouts require explicit format and
sign confirmations. The confirmed date format reparses ambiguous raw transaction
and posting dates unless the user supplied a corrected value for that field.
Every uncertain row needs a confirm-or-reject decision, and confirmed corrections
must satisfy the canonical transaction contract. A correction cannot change the
extracted account, currency, category, or financial role. Extracted balance
fields must each be explicitly confirmed or corrected. Extracted statement
coverage must also be confirmed or corrected; when balance evidence exists, an
approved period is mandatory even if extraction did not find one. Confirmed
transactions cannot sit outside that period or inside an explicit coverage gap.
A balance mismatch requires explicit acknowledgement.

`ApprovedStatement` is the only review output eligible for later trusted use.
Its approved rows retain the source identity and fingerprint, original values,
extracted draft, provenance, OCR lines and field confidence, issues and review
reasons beside the final canonical transaction. Its rejected rows retain their
complete unchanged `StatementReviewRow` evidence as well as a fingerprint index.
It also retains raw balance evidence, explicitly confirmed or corrected balances,
whether those balances changed, confirmed statement coverage and whether it
changed, final reconciliation, and approval time. The balances and period remain
available when reconciliation is `unavailable`, so later persistence can date a
closing snapshot from the statement end rather than the last transaction. This
contract is an in-memory service result; it neither persists transactions nor
defines a UI.

## Duplicate and statement-overlap results

`DuplicateAssessment` has `unique`, `probable`, and `exact` states. Their only
valid actions are respectively `keep`, `review`, and `skip`. Only the same
source record or the same non-empty bank external ID on one account produces an
exact result. A probable result can be produced by equal amounts, similar
descriptions, and dates no more than two days apart. Distinct external IDs cap
the score below the probable threshold to protect legitimate repeated
purchases.

`RepeatedFileAssessment` compares exact document hashes.
`StatementOverlapAssessment` separately reports no, partial, or exact inclusive
date-range overlap for statements on the same account. Overlap never silently
removes a transaction.

## Relational persistence contracts

The initial SQLite migration creates tables for the user profile, accounts,
import batches and context, statement coverage, balance snapshots, raw and
verified transactions, categories, financial roles, user flags, category
corrections, recurring series, budgets, savings goals, scenarios, forecast runs,
anomaly alerts, and model metadata.

Money columns use `NUMERIC(18, 2)` and return `Decimal`. Verified transaction
constraints reject zero amounts and sign/direction disagreement. Timestamps are
accepted only when timezone-aware, stored as UTC, and restored with explicit UTC
awareness. Dates without times use SQL `DATE`.

Database constraints supplement rather than replace Pydantic validation. They
enforce supported account/source/status values, statement and forecast date
ordering, exact file/source-fingerprint uniqueness, foreign-key integrity, and
one verified record per raw transaction. Deleting the local profile cascades to
its private financial records; stable lookup rows use restrictive or nulling
foreign keys where silent deletion would damage meaning.

Rejected raw rows may omit a canonical fingerprint because invalid values must
not be converted into a fabricated canonical identity. Their exact source
fingerprint, source location, raw payload, original mapped text, parser version,
review state, and structured issues remain mandatory. Both opening and closing
statement balances are persisted as balance snapshots, never as transactions.

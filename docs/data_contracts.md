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

## Deterministic categorisation

`configs/category_rules.yaml` is a separately versioned deterministic rule set
and declares the taxonomy version it targets. `CategoryRuleSet` validates unique
rule identities, exact merchant aliases, and keyword phrases; the taxonomy-aware
loader also validates that configured category targets exist and are active.
Merchant aliases and keyword phrases must remain unique after Unicode, case,
punctuation, and whitespace normalisation, so configuration order cannot silently
select a different category.

`ScopedCategoryRule` represents one caller-supplied personal rule owned by the
selected profile. It always requires an exact normalised merchant and can also
require direction, account, a whole description phrase, and an inclusive
absolute-amount range. Optional restrictions are combined with AND semantics.
Amounts remain `Decimal`, range bounds must be non-negative, and the maximum
cannot be below the minimum. An inactive personal rule cannot match.

`CategorisationPlan` limits one run to an owned profile and either every verified
transaction in that profile or a non-empty unique transaction selection. Its
personal-rule identities must be unique and every rule must name the same
profile. Personal rules are inputs to the run; this contract does not persist or
create them.

`CategoryDecision` records the transaction, previous and selected category,
taxonomy and rule-set versions, whether the stored category changed, and a
`CategoryExplanation`. Explanations use controlled sources and reason codes,
optionally identify the selected rule, and identify only the fields that matched.
They never copy merchant text, descriptions, amounts, or account values.

The current precedence sources are transaction decision, personal rule, merchant
mapping, keyword rule, and `needs_review`. A transaction decision is the latest
existing explicit category correction. If no deterministic rule matches, or
equally ranked rules disagree, the selected category is `needs_review`. Commit 19
adds a separately evaluated model candidate without changing this service;
Commit 20 adds hybrid inference, persistent personal-rule creation, and the
correction workflow. This deterministic stage defines neither an API nor a UI
contract.

## ML categorisation training and evaluation

The ML boundary uses immutable typed requests and results rather than passing an
unvalidated DataFrame between persistence, evaluation, and artefact storage.
Its training plan identifies the owned profile, taxonomy and model versions,
overall aware knowledge cutoff, historical chronological boundary, explicit
minimum sample policy, deterministic merchant-holdout policy, and random seed.
Every threshold affecting dataset sufficiency or model selection is visible in
that plan or its recorded metadata.

A private training example contains only the transaction identity needed for
stable processing, transaction date, verified merchant and description,
normalised merchant group, taxonomy category, verification time, and category
correction history needed for as-of reconstruction. Examples are transient and
must not be serialised into training metadata. The dataset result instead
returns aggregate eligible and excluded counts with controlled reason codes.

An eligible supervised label must be an explicit category correction available
at the relevant knowledge cutoff. The current value of
`verified_transactions.category_id` is not an as-of label because automatic
assignment has no historical timestamp. A later correction is permitted as test
truth at the overall evaluation cutoff, but it cannot enter a chronological
training fold whose historical cutoff predates that correction. Corrections
with identical times use their stable database identity as the final tie-break.

Financial-role eligibility is reconstructed from the latest
`FinancialRoleAuditRecord` available at the same cutoff. An absent audit means
`unknown`; the current `financial_role_id` is not used as historical truth.

Eligibility also requires:

- an owned verified transaction and confirmed raw-row lineage;
- a fully verified source document for digital-PDF and OCR-derived rows;
- no exact or unresolved probable-duplicate evidence;
- no unresolved transfer review or structured `needs_review` flag;
- a financial role other than `unknown` or `excluded`; and
- an active taxonomy category other than `needs_review`.

These filters determine whether a row can teach the classifier; they do not
delete or rewrite any retained source evidence. In particular, statement notes,
raw payloads, extraction confidence, amounts, balances, and account identifiers
are not model features.

The versioned feature schema converts temporary normalised merchant and
description values into one marked text value. A fitted model combines word
TF-IDF and character-within-word TF-IDF with Logistic Regression. The model
input contract rejects text that is empty after normalisation. Standalone batch
prediction preserves input order and returns the estimator's category classes
and probabilities; it does not choose a confidence threshold or create a
`CategoryDecision`.

Each holdout result names the split and records training/test counts and ranges,
class and merchant-group diagnostics, metrics for both the candidate and a
most-frequent-category baseline, and a labelled confusion matrix. Metrics include
macro F1, weighted F1, precision, recall, and per-class support. The evaluation
result records whether the explicit candidate-selection rule passed, but that
flag is not model activation.

Artefact metadata contains only controlled configuration and aggregates: model,
taxonomy and feature-schema versions; training period and cutoffs; class counts;
split policy; parameters and seed; baseline and candidate metrics; software
versions; creation time; separate aggregate exclusion counts for the historical
and final datasets; selection result; and a SHA-256 artefact checksum. It
must not contain example text, merchant names, or transaction, profile, and
account identifiers. The model and its JSON sidecar are private local files,
not API payloads or database records in this stage.

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

The persisted source values are statement opening, statement closing, running
balance, and manual. Imported opening and closing balances use the explicitly
confirmed statement coverage boundaries. A running balance from an accepted
unique CSV row uses its posting date when present and its transaction date
otherwise. A manual entry is its own verified observation and supplies neither
statement coverage nor transaction history.

When selecting current balance evidence, the greatest non-future `as_of_date`
wins. Same-date ties use manual, statement closing, running balance, then
statement opening priority before recording time and database identity. Only
verified records are eligible. Amounts stay as fixed-precision `Decimal`; zero
and negative balances are valid.

## Financial-data freshness

A freshness request identifies one account, an assessment date, and an explicit
policy. The policy supplies maximum permitted transaction age, balance age, and
coverage age in days, plus the minimum consecutive verified coverage in days.
These thresholds are caller decisions rather than hidden defaults.

The result exposes the latest eligible verified-transaction date and its age,
the selected balance amount, source, date, recording time and age, and the latest
qualifying consecutive coverage range, length and age. It also exposes
`data_freshness_days`, defined as the age of the more recent of the latest
trusted transaction and selected balance. The separate ages remain available so
a recent manual balance cannot conceal stale transaction history.

Coverage can prove continuity only when it belongs to a verified import and is:

- `complete` or `overlapping`, contributing its inclusive whole period; or
- `gapped`, contributing only the known segments outside every explicit missing
  period.

`partial` and `unknown` coverage contribute no known interval. Adjacent or
overlapping eligible intervals can form one consecutive range. Future
transactions, balances, or coverage do not satisfy freshness, and absence is
reported as unknown rather than zero.

The assessment returns stable warnings and either `active_forecasting` or
`archive`. `active_forecasting` means only that every explicit evidence-age and
continuity rule passed. The contract does not produce a forecast, change stored
records, or define an API or interface.

Warning codes are `account_inactive`, `no_verified_transactions`,
`transactions_stale`, `no_verified_balance`, `balance_stale`,
`no_verified_coverage`, `coverage_stale`,
`insufficient_contiguous_coverage`, and
`latest_transaction_outside_contiguous_coverage`. Their order is deterministic:
account state, transaction evidence, balance evidence, then coverage evidence.

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

## Financial-role review contracts

`FinancialRoleSuggestion` records a subject transaction, optional transfer
counterpart, controlled kind and reasons, proposed role or paired roles,
confidence, algorithm version, status, and review timestamps. Pending
suggestions have no review time; confirmed or rejected suggestions require one.
Refund and reimbursement suggestions cannot claim a counterpart, while paired
transfers require distinct transactions and opposite transfer directions.

`TransactionReviewAction` exposes income, expense, internal transfer, refund,
reimbursement, cash withdrawal, exclusion from analytics, and `needs_review`.
Positive amounts accept income, transfer-in, refund, or reimbursement roles;
negative amounts accept expense, transfer-out, or cash-withdrawal roles.
`excluded` is valid for either sign. A needs-review action adds a structured flag
without changing the role.

`FinancialRoleAudit` retains the previous and new roles, transaction, optional
confirmed suggestion, decision source, and aware change time. `RoleReviewItem`
shows current transaction data plus statement flags and note as inert local
reference context. Free text is not a rule input and never appears in controlled
reason codes.

The `reviewed_at` and `changed_at` service arguments must be timezone-aware for
boundary compatibility, but stored suggestion reviews, role audits, and
`needs_review` flags use one authoritative server UTC receipt time per operation.
That time must not precede the relevant verification, suggestion, or existing audit.
The current schema has no separate caller-reported-time field, so the supplied value
is deliberately not persisted and cannot make a decision visible to an earlier
knowledge cutoff.

## Coverage-aware analytics contracts

`AnalyticsScope` identifies the local profile, a non-empty unique account set,
an inclusive `DateRange`, the account or consolidated view, and a bounded largest
transaction count. Account view requires exactly one account. The service rejects
missing or foreign-owned accounts and account sets that do not share one currency.

`DataCoverageIndicator` keeps the requested range beside fully covered,
partially covered, and missing ranges with inclusive day counts. It also includes
one `AccountCoverageIndicator` per account so consolidated gaps remain
explainable. Its status is `complete`, `partial`, or `missing`.

`CashFlowTotals` contains income, expenses, refunds, reimbursements, cash
withdrawals, external net cash flow, visible transfer inflow/outflow/net movement,
unknown inflow/outflow, excluded inflow/outflow, and controlled counts. Its value
basis is `complete_period` or `observed_only`. The totals contract is absent when
the requested period has no trusted coverage; observed transaction count remains
available to show that rows were not silently discarded.

`SavingsRateResult` contains either a two-decimal percentage or exactly one
stable reason: incomplete coverage, unresolved financial roles, or no income.
`CategorySpending` groups expense-role rows only and preserves `None` as the
uncategorised bucket. `SpendingCadenceBreakdown` exposes recurring,
discretionary, and unclassified values; until the later recurrence stage creates
an explicit transaction classification, all expense-role spending is
unclassified.

`LargestTransaction` retains the current role and signed amount so transfers and
unknowns are explainable; explicitly excluded rows are omitted. Balance contracts
return per-account `BalanceHistorySegment` values. A segment has either a proven
coverage interval or one standalone point, and callers must not connect separate
segments.

`MonthlyCashFlow` distinguishes a complete empty month from an uncovered month by
using a present zero-valued total only for the former. `MonthlyComparison` returns
changes only for adjacent full calendar months with complete coverage and no
unknown roles; otherwise it carries a stable unavailability reason.

## Hybrid categorisation

`HybridCategorisationPlan` requires an owned profile and explicit probability
threshold. `HybridCategoryDecision` distinguishes applied from pending review and
exposes controlled source, confidence, model version, and explanation fields.
`CategoryFeedback` requires a timezone-aware audit time and either
`transaction_only` or `create_personal_rule`; the latter requires the complete
rule rather than inferring one. The reported time must be no earlier than
transaction verification or the latest decision and must be strictly later than
the latest correction; future timestamps fail closed. The server receipt time is
persisted as the authoritative cutoff-visible time. A requested personal rule must
match the selected transaction on every supplied scope. An applied resolution
supersedes older pending review records. `ManualRetrainingDataset` preserves its
Commit 19 knowledge cutoff.

## Recurrence contracts

`RecurrenceDetectionPolicy` makes occurrence, amount, interval, and confidence
thresholds explicit. `RecurringPaymentCandidate` carries account and controlled
merchant grouping, currency, direction, financial role, frequency, signed amount,
chronological evidence dates, next date, confidence, covered misses, review status,
`evidence_as_of_date`, and timezone-aware `knowledge_cutoff_at`. Its next date must
follow both the latest occurrence and evidence date. The persistence member link adds
`identified_at`, so recurrence membership can be queried as of a past cutoff.
`RecurrenceReview` records explicit confirmation or cancellation with an aware audit
time.

## Forecast-data contracts

`DailyForecastObservation` uses `0.00` for a covered zero-spend day and null values for
an unknown day. A covered value also requires a timezone-aware `known_at` no earlier
than the end of its UTC calendar day. `WeeklyForecastTarget` requires a complete
Monday-to-Sunday week and retains the latest `known_at` evidence across that week.
`ForecastFeatureRow` carries a Monday UTC `forecast_origin_at`, lag 1/2/4, rolling
4/8, payday distance, calendar, known recurring-flow inputs, and `target_known_at`.
The dataset contract binds those timestamps to the explicit knowledge cutoff and
requires row targets to retain their weekly availability evidence.
`RecurringOutflowProjection` binds the next Monday's aggregate confirmed recurring
amount to the cutoff at which that projection was known; a zero therefore represents
cutoff-verified absence, not an arbitrary caller default.

Baseline evaluation records both rolling expanding-validation metrics and final-test
metrics, including predictions, MAE, RMSE, and signed bias. Fold contracts require
training weeks to strictly precede their test week.

`ForecastModelPolicy` makes sample requirements, final-test size, relative MAE
improvement, allowed RMSE regression, allowed absolute-bias increase,
gradient-boosting parameters, and seed explicit. `ForecastInferenceRow` contains only
features for one future Monday: it has no target field and its month, ISO week, and
UTC origin must match that date. Its recurring-flow amount carries an aware evidence
time strictly before the origin. `ForecastModelComparison` records the executable
selected model or baseline, justification, optional low-data versus complete
evaluation evidence, separate final and expanding baseline references, horizon-one
performance, signed controlled permutation importance, chronological dates, the
complete model policy, training cutoff, and eligible sample count.
`ForecastPrediction` is one non-negative weekly discretionary-spending amount plus
its forecast origin, selected model identity, selection state, and training cutoff.

## Forecast-path contracts

`ForecastPathPlan` binds one owned account, Monday origin, one-to-365-day horizon,
aware knowledge cutoff, explicit freshness limits, interval probability, simulation
count, minimum residual scale, widening multipliers, and random seed.
`RecurringForecastOccurrence` contains only a candidate identity, date, signed amount,
cash-affecting financial role, and evidence time. `ForecastOpeningBalance` retains the
verified balance amount, currency, observation date, recording time, and source.

`ForecastScenario` supports a non-negative discretionary multiplier and uniquely
identified one-off signed inflow/outflow adjustments. `WeeklySpendingPath` and
`DailyBalancePathPoint` require ordered point estimates inside their lower/upper
bounds. `BalanceForecastPath` covers every requested date and carries the selected
model, residual method, widening factor, stable warnings, source freshness warnings,
confirmed recurring occurrences, held-out interval performance, and a final summary
that must equal the last daily point.

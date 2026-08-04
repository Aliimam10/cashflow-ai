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

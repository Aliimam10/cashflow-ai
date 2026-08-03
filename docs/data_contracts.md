# Data Contracts

Canonical transaction, import, money, date, and model-metadata contracts will be
defined in the dedicated data-contract stage.

Foundational invariants:

- income and refunds are positive;
- expenses and outgoing transfers are negative;
- stored money uses fixed precision;
- API dates use ISO 8601;
- timestamps are timezone-aware;
- raw source rows are preserved;
- malformed required values are rejected with explicit reasons.

## Planned import-source contract

Commit 5 will represent the source type as CSV, digital PDF, or OCR-derived PDF.
Every provisional row will carry:

- source document and import-batch identifiers;
- page number and source region where applicable;
- preserved raw text or row payload;
- extraction method;
- field-level or row-level confidence;
- warnings and validation errors;
- user-review status.

PDF extraction produces provisional values, not accepted transactions. Required
dates, descriptions, amounts, signs, and balances remain editable during review.
User confirmation is mandatory before the normal cleaning and persistence flow.
An optional CSV representation may be exported for inspection, but CSV is not
the internal contract between PDF extraction and validation.

## Synthetic demonstration records

The demo-data generator currently exposes a typed provisional transaction
record. It uses the required sign convention, ISO-formatted dates, two-decimal
GBP values, and explicit truth labels for categories, recurrence, anomalies, and
duplicate examples. Commit 5 will define the canonical application schemas;
those schemas, not the provisional generator record, will become the ingestion
boundary.

An exact duplicate row repeats its source transaction and does not affect the
reconstructed balance. A probable duplicate is a separate debit and remains in
the balance unless a later deterministic import rule proves otherwise.

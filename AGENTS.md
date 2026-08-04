# CashFlow AI Agent Guide

This file defines repository-wide working conventions. Read it with `README.md`
and the relevant document under `docs/` before changing the project.

## Architecture and module boundaries

CashFlow AI is a local-first modular monolith. Planned components are a FastAPI
backend, a Streamlit frontend, domain services, SQLAlchemy persistence, and
separately versioned model artefacts.

- Put reusable application and domain logic under `src/cashflow_ai/`.
- Keep HTTP routes and Streamlit pages thin; they must delegate business logic
  to application or domain services.
- Keep data ingestion, analytics, categorisation, recurrence, forecasting,
  anomaly detection, and financial planning as explicit modules.
- Keep CSV parsing, digital-PDF extraction, and OCR as source adapters that
  produce the same canonical transaction candidates.
- Keep transaction category separate from financial role so transfers, refunds,
  reimbursements, and cash withdrawals are not misreported as income or expense.
- Use typed schemas or entities at module boundaries instead of passing
  unvalidated DataFrames throughout the application.
- Do not replace the modular-monolith architecture or introduce microservices
  without explicit approval.

## Stage discipline

- Inspect the repository before making changes.
- Implement only the requested stage; do not pull later features forward.
- Describe the objective, affected files, approach, and important edge cases
  before implementation.
- Add or update tests and documentation with every behavioural change.
- Run the relevant validation commands and inspect the final diff.
- Do not create commits unless explicitly requested.

## Developer commands

Use the `uv`-managed environment for all project commands:

```bash
make setup
make format
make format-check
make lint
make typecheck
make test
make pre-commit
make check
make check-import
```

Run `make format` while editing. Run `make check` and `make pre-commit` before
handoff. Do not bypass failing checks; fix the cause or document a justified,
reviewed exception.

## Money and date conventions

- Store money using fixed-precision decimal database types.
- Represent money in APIs as decimal strings or documented minor currency units.
- Convert to floating point for ML features only through an explicit copy.
- Use positive amounts for income and refunds, and negative amounts for expenses
  and outgoing transfers.
- Use ISO 8601 in APIs, date types for dates without times, and timezone-aware
  timestamps. Make timezone conversions explicit.
- Treat uncovered statement dates as unknown data, never as zero spending.
- Treat a balance snapshot as evidence of an account balance, never as a
  synthetic transaction.

## Privacy and source-data preservation

- Never commit real bank statements, credentials, private transaction data,
  generated uploads, local databases, or secrets.
- Real statements used locally must remain in ignored directories and must not
  appear in fixtures, screenshots, logs, or model artefacts committed to Git.
- Do not log raw transaction descriptions in normal application logs.
- Preserve every source row in an auditable raw-import record.
- Preserve extraction provenance and confidence for values derived from PDFs.
- Never persist PDF- or OCR-derived transactions until the user has reviewed
  the extraction preview and explicitly confirmed the import.
- Prefer embedded text/table extraction for digital PDFs; use OCR only for
  image-based pages or when reliable text extraction is unavailable.
- Store free-text statement notes as reference metadata only. Notes must not
  directly change categories, financial roles, analytics, or forecasts.
- Quarantine malformed rows with useful errors; never silently discard them.
- Automatically exclude only deterministic exact duplicates. Flag probable
  duplicates for review.

## ML and forecasting safeguards

- Never use information created after a historical cutoff in training, feature
  engineering, recurring-payment detection, or evaluation.
- Fit preprocessing and recurrence detection independently within each
  time-aware training fold.
- Use chronological evaluation for forecasting; never shuffle time-series data.
- Implement and report meaningful simple baselines before advanced models.
- Select an advanced model only when it consistently improves relevant metrics
  and has acceptable failure behaviour, uncertainty, latency, and explainability.
- Separate confirmed recurring flows from predicted discretionary cash flow.
- Persist reproducibility and model metadata with every model artefact.

## Definition of done

A change is complete only when its acceptance criteria are met, relevant tests
pass, error behaviour and public interfaces are documented, and the diff contains
no unrelated changes or private data.

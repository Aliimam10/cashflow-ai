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

- CSV adapters parse encodings, infer column mappings, and preserve each row.
- Digital-PDF adapters prefer embedded text and table structure from statements
  downloaded from an online banking application.
- OCR adapters handle scanned or camera-captured PDF pages and retain page,
  region, and recognition-confidence metadata.

Adapters do not categorise transactions, calculate analytics, or write accepted
transactions directly to the database. All sources pass through the same
canonical validation rules after the user reviews the extraction preview.

PDF extraction is a two-stage decision: attempt digital extraction first, then
fall back to OCR only for pages without usable embedded text. Low-confidence,
ambiguous, or unreconciled rows are highlighted. No PDF-derived transaction is
accepted until the user explicitly confirms the preview.

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

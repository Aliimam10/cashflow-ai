# Architecture

CashFlow AI will use a local-first modular-monolith architecture. This document
will describe component boundaries, dependency direction, deployment topology,
and important architectural decisions as those components are implemented.

The approved high-level flow is:

```text
data sources -> ingestion -> relational storage -> analytics and ML
             -> FastAPI -> Streamlit
```

Business logic will remain outside API routes and Streamlit pages.

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

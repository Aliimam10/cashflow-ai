# CashFlow AI

CashFlow AI is a planned local-first personal cash-flow forecasting, budgeting,
and financial-insight application. It will import transaction CSV files and
digital or scanned PDF bank statements, normalise and categorise transactions,
identify recurring activity and unusual transactions, and produce explainable
balance forecasts with uncertainty.

The repository currently contains the **project foundation, quality tooling,
typed configuration, structured logging, reproducible synthetic demo data,
canonical transaction and statement contracts, safe CSV preview and mapping,
transaction normalisation, conservative duplicate/overlap detection, a migrated
local SQLite persistence layer, atomic confirmed CSV imports, and review-only
embedded-text PDF extraction**. OCR, PDF persistence, APIs, user interfaces,
and machine-learning components have not been implemented yet.

## Problem

Transaction histories explain what has already happened but often do not show
how upcoming commitments and ordinary spending may affect future balances.
CashFlow AI is intended to turn imported history into an understandable,
forward-looking decision-support view while keeping assumptions, uncertainty,
and model limitations visible.

## Planned architecture

The application will be a Python modular monolith:

```text
CSV, digital PDF, scanned PDF, and synthetic demo data
                       |
                       v
        Source extraction and user confirmation
                       |
                       v
          Validation and normalisation
            |
            v
Relational database
       +----+----+
       |         |
       v         v
  Analytics   ML pipelines and artefacts
       \         /
        v       v
       FastAPI backend
             |
             v
      Streamlit frontend
```

Version 1 uses SQLite locally and in the Docker environment. PostgreSQL is
postponed until the local single-user application is complete. FastAPI and
Streamlit will be added in later, separately reviewed stages.

### Planned statement import

- CSV exports can now be decoded and structurally validated into a limited,
  non-persistent preview. Common headings produce mapping suggestions, while the
  user-selected mapping supports either a signed amount or separate debit and
  credit columns.
- Mapped rows can be normalised into provisional drafts while retaining exact
  source text, parser identity, source fingerprint, and matching fingerprint.
  Exact duplicates can be distinguished from probable matches requiring review.
- A confirmed CSV can now be stored atomically. Every row is preserved; unique
  valid rows become verified transactions, exact duplicates are skipped,
  probable duplicates await review, and invalid rows retain structured issues.
  Statement context, gaps, overlaps, notes, flags, and reported balances are
  stored with the import, and an unexpected failure rolls everything back.
- Digital PDFs downloaded from online banking can now be validated and parsed
  in memory using embedded text, recognised tables, or a conservative generic
  fallback. Candidates retain their source page and require review; no PDF row
  is persisted yet.
- Camera-captured or scanned PDFs will use OCR and retain confidence and source
  location for extracted values.
- Every PDF produces a reviewable draft. The user must confirm the recognised
  dates, descriptions, amounts, and balances before transactions are imported.
- All three input paths converge on the same canonical transaction contracts;
  PDFs are not trusted merely because they can be converted to tabular text.

The CSV preview, confirmation, and persistence pipeline currently consists of
Python services rather than an upload or review screen.
Text-based PDF preview extraction is also a Python service. OCR and the shared
PDF confirmation/persistence workflow remain later stages.

## Development setup

Prerequisites:

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

Install the project and development dependencies:

```bash
make setup
```

Copy the safe local configuration template if you need environment overrides:

```bash
cp .env.example .env
```

Configuration keys use the `CASHFLOW_` prefix. Development defaults work
without a `.env` file.

Run the current test suite:

```bash
make test
```

Format the code and run all quality checks:

```bash
make format
make check
make pre-commit
```

Verify that the package imports:

```bash
make check-import
```

Create or upgrade the local SQLite database:

```bash
make db-upgrade
```

The default is `data/cashflow.db`. Override it locally with
`CASHFLOW_DATABASE_URL`; Version 1 accepts local SQLite URLs only. Database files
are private runtime data and ignored by Git. `make db-downgrade` removes all
application tables and should be used only when intentionally discarding the
local schema.

Generate all three synthetic demonstration profiles using the default seed:

```bash
make demo-data
```

Generated files are written under `data/demo/generated/` and are intentionally
ignored because they can be reproduced from source. Run the CLI with `--help`
to select a profile, date range, seed, output directory, or CSV layout.

## Privacy

This repository must never contain real bank statements, credentials, personal
transaction histories, or secrets. Synthetic data will be used for committed
examples and automated tests. Local uploads, databases, processed private data,
and model artefacts are excluded from version control.

See [`docs/privacy.md`](docs/privacy.md) for the evolving privacy design.

## Documentation

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/data_contracts.md`](docs/data_contracts.md)
- [`docs/imports.md`](docs/imports.md)
- [`docs/persistence.md`](docs/persistence.md)
- [`docs/modelling.md`](docs/modelling.md)
- [`docs/evaluation.md`](docs/evaluation.md)
- [`docs/privacy.md`](docs/privacy.md)

## Status and roadmap

The planned implementation is deliberately incremental. The project foundation,
quality tooling, typed settings, structured logging, privacy-safe synthetic
data, canonical data contracts, CSV preview/mapping, transaction cleaning,
duplicate/statement-overlap detection, and SQLite persistence are configured.
Confirmed CSV imports and text-PDF extraction are implemented. The next stages
will add scanned-PDF OCR and statement review, analytics, evaluated ML
components, APIs, the frontend, deployment, and release documentation.

No feature listed here should be considered available until its implementation
and evaluation are present in the repository.

## Disclaimer

> CashFlow AI provides estimates based on historical and user-supplied
> information. Forecasts may be inaccurate and are not financial advice.

## Licence

This project is licensed under the [MIT License](LICENSE).

# CashFlow AI

CashFlow AI is a planned local-first personal cash-flow forecasting, budgeting,
and financial-insight application. It will import transaction CSV files and
digital or scanned PDF bank statements, normalise and categorise transactions,
identify recurring activity and unusual transactions, and produce explainable
balance forecasts with uncertainty.

The repository currently contains the **project foundation, quality tooling,
typed configuration, structured logging, reproducible synthetic demo data,
canonical transaction and statement contracts, and safe CSV preview and column
mapping**. Transaction cleaning, accepted-import persistence, PDF parsing, APIs,
user interfaces, and machine-learning components have not been implemented yet.

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
- Digital PDFs downloaded from online banking will use embedded text and table
  extraction where possible.
- Camera-captured or scanned PDFs will use OCR and retain confidence and source
  location for extracted values.
- Every PDF produces a reviewable draft. The user must confirm the recognised
  dates, descriptions, amounts, and balances before transactions are imported.
- All three input paths converge on the same canonical transaction contracts;
  PDFs are not trusted merely because they can be converted to tabular text.

The CSV preview is currently a Python service rather than an upload screen and
does not yet create accepted transactions. PDF ingestion is planned Version 1
functionality but is not implemented in the current repository stage.

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
- [`docs/modelling.md`](docs/modelling.md)
- [`docs/evaluation.md`](docs/evaluation.md)
- [`docs/privacy.md`](docs/privacy.md)

## Status and roadmap

The planned implementation is deliberately incremental. The project foundation,
quality tooling, typed settings, structured logging, privacy-safe synthetic
data, canonical data contracts, and CSV preview/mapping adapter are configured.
The next stages will add transaction cleaning, import persistence, PDF source
adapters, analytics, evaluated ML components, APIs, the frontend, deployment,
and release documentation.

No feature listed here should be considered available until its implementation
and evaluation are present in the repository.

## Disclaimer

> CashFlow AI provides estimates based on historical and user-supplied
> information. Forecasts may be inaccurate and are not financial advice.

## Licence

This project is licensed under the [MIT License](LICENSE).

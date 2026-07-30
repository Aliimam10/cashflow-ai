# CashFlow AI

CashFlow AI is a planned local-first personal cash-flow forecasting, budgeting,
and financial-insight application. It will import transaction CSV files,
normalise and categorise transactions, identify recurring activity and unusual
transactions, and produce explainable balance forecasts with uncertainty.

The repository is currently at the **project foundation** stage. Financial
features, APIs, persistence, user interfaces, and machine-learning components
have not been implemented yet.

## Problem

Transaction histories explain what has already happened but often do not show
how upcoming commitments and ordinary spending may affect future balances.
CashFlow AI is intended to turn imported history into an understandable,
forward-looking decision-support view while keeping assumptions, uncertainty,
and model limitations visible.

## Planned architecture

The application will be a Python modular monolith:

```text
CSV and synthetic demo data
            |
            v
Import, validation, and normalisation
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

The planned local database is SQLite, with PostgreSQL used by the full Docker
environment. FastAPI and Streamlit will be added in later, separately reviewed
stages.

## Development setup

Prerequisites:

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

Install the project and development dependencies:

```bash
make setup
```

Run the current test suite:

```bash
make test
```

Verify that the package imports:

```bash
make check-import
```

## Privacy

This repository must never contain real bank statements, credentials, personal
transaction histories, or secrets. Synthetic data will be used for committed
examples and automated tests. Local uploads, databases, processed private data,
and model artefacts are excluded from version control.

See [`docs/privacy.md`](docs/privacy.md) for the evolving privacy design.

## Documentation

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/data_contracts.md`](docs/data_contracts.md)
- [`docs/modelling.md`](docs/modelling.md)
- [`docs/evaluation.md`](docs/evaluation.md)
- [`docs/privacy.md`](docs/privacy.md)

## Status and roadmap

The planned implementation is deliberately incremental. The next stages will
add quality tooling, typed settings, reproducible synthetic data, canonical data
contracts, ingestion and persistence, analytics, evaluated ML components, APIs,
the frontend, deployment, and release documentation.

No feature listed here should be considered available until its implementation
and evaluation are present in the repository.

## Disclaimer

> CashFlow AI provides estimates based on historical and user-supplied
> information. Forecasts may be inaccurate and are not financial advice.

## Licence

This project is licensed under the [MIT License](LICENSE).


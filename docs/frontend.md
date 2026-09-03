# Local Streamlit frontend

Commit 32 introduces the first user-facing shell without moving financial logic out
of the existing FastAPI and domain boundaries. It provides:

- a home page explaining the product and showing local API/database readiness;
- stable navigation for home, statement import, transactions/analytics, and
  forecast/planning;
- common loading, controlled-error, empty-state, privacy, and forecast-warning copy;
- a typed API client that validates public response contracts; and
- data-minimised session state for navigation plus optional local record identifiers.

The statement, transaction, and forecast destinations are intentional placeholders.
They do not claim that Commit 33–35 workflows already exist.

## Local boundary

Both processes are restricted to a loopback address. `CASHFLOW_API_HOST` and
`CASHFLOW_UI_HOST` accept only `127.0.0.1`, `localhost`, or `::1`; their ports accept
1–65535. The frontend client accepts only explicit local HTTP URLs and relative API
paths. It does not inherit system proxy settings, retain response bodies in errors, or
access the database and domain services directly. The packaged launcher also runs
headless and disables Streamlit usage-stat collection.

Streamlit state contains only the selected page, optional profile/account identifiers,
and a display preference. It does not retain statement bytes, descriptions, amounts,
balances, transactions, API responses, model inputs, or forecasts. This local-only
design is not authentication: do not expose either process to another machine.

## Manual verification

This check uses an empty local database; no statement or personal data is required.
In the first terminal:

```bash
make setup
make db-upgrade
make api
```

Expected API output includes a Uvicorn message stating that it is running on
`http://127.0.0.1:8000`.

In a second terminal:

```bash
make ui
```

Open `http://127.0.0.1:8501`. Expected behaviour:

1. **Home** displays “CashFlow AI”, the privacy notice, forecast disclaimer, API
   status `ok`, and database status `ready`.
2. Selecting **Import statements**, **Transactions & analytics**, or **Forecast &
   planning** changes the page without an exception.
3. Each unfinished page clearly says that it is not implemented yet; the forecast
   page repeats the forecast disclaimer.
4. Stopping the API and refreshing **Home** displays a controlled unavailable message
   and `connection_failed`, without a traceback or response body.

Safe parameters to vary in a local `.env` are `CASHFLOW_API_PORT` and
`CASHFLOW_UI_PORT`, provided each process uses the matching API port and both ports are
free. The host values must remain loopback addresses.

## Current limitations

- There is no profile/account form or statement upload/review screen yet.
- Transaction, analytics, categorisation, role-review, forecast, planning, and anomaly
  API capabilities do not yet have interactive pages.
- The client is synchronous, which is suitable for the current small local status
  request; longer later workflows must keep visible loading and controlled failures.
- Loopback restriction is a privacy safeguard, not user authentication or a deployment
  design.

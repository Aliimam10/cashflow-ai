# Local HTTP API

Commit 30 exposes the existing profile, account, statement-ingestion, and
verified-transaction services through a local FastAPI application. The API is an
adapter around existing application and domain code: routes validate HTTP input,
resolve dependencies, call a service, and return a typed response. They do not
reimplement parsing, reconciliation, persistence, or financial calculations.

This is not yet the end-user interface. The later Streamlit frontend will call
these endpoints instead of accessing the database or domain services directly.
Analytics, categorisation, recurrence, forecasting, anomaly, model-registry,
planning, and scenario endpoints belong to Commit 31.

## Running locally

Install the environment and create the current SQLite schema before starting the
server:

```bash
make setup
make db-upgrade
make api
```

The default address is `http://127.0.0.1:8000`. Interactive OpenAPI documentation
is available at `/docs`, alternative documentation at `/redoc`, and the machine-
readable schema at `/openapi.json`.

`CASHFLOW_API_HOST` accepts only `127.0.0.1`, `localhost`, or `::1`, and
`CASHFLOW_API_PORT` accepts ports 1 through 65535. This loopback-only default is a
privacy boundary, not user authentication. A later stage must add an explicit
authentication and deployment design before exposing the API to another machine.

The application does not run Alembic automatically. `/health` can therefore be
healthy while `/ready` reports HTTP 503 until `make db-upgrade` has created the
required schema. Optional OCR availability is reported separately because CSV and
digital-PDF use must not fail merely because Tesseract is absent.

## Routes in this stage

| Method and path | Purpose | Persistence |
| --- | --- | --- |
| `GET /health` | Process liveness and package version | None |
| `GET /ready` | Database connection and required-table readiness | None |
| `POST /api/v1/profiles` | Create the single local profile | Profile metadata |
| `GET /api/v1/profiles/current` | Read the current local profile | None |
| `GET /api/v1/profiles/{profile_id}` | Read one local profile | None |
| `POST /api/v1/profiles/{profile_id}/accounts` | Create current/checking or savings metadata | Account metadata |
| `GET /api/v1/profiles/{profile_id}/accounts` | List the profile's accounts | None |
| `GET /api/v1/accounts/{account_id}` | Read one account | None |
| `POST /api/v1/imports/csv/preview` | Validate and preview an uploaded CSV | None |
| `POST /api/v1/imports/csv/confirm` | Revalidate and atomically import an exact confirmed CSV | Confirmed import and retained source rows |
| `POST /api/v1/imports/pdf/text/preview` | Extract an embedded-text PDF for review | None |
| `POST /api/v1/imports/pdf/ocr/preview` | Extract a scanned PDF with local OCR | None |
| `GET /api/v1/ocr/status` | Report local Tesseract availability | None |
| `POST /api/v1/imports/pdf/review` | Re-extract a PDF and prepare targeted review decisions | None |
| `POST /api/v1/imports/pdf/confirm` | Re-extract a PDF and apply explicit approval in memory | None |
| `GET /api/v1/imports/{import_batch_id}/context` | Read stored coverage, balances, flags, and inert notes | None |
| `GET /api/v1/accounts/{account_id}/transactions` | List verified transactions | None |
| `GET /api/v1/transactions/{transaction_id}` | Read one verified transaction | None |

List routes intentionally return complete local collections in this first API
stage. Pagination, filtering, and sorting controls are Commit 31 work and are
required before collections can grow without bound.

## Upload and confirmation contracts

Uploads use `multipart/form-data` and are read into bounded memory. CSV and PDF
limits remain owned by the existing import modules. The API closes each uploaded
file after reading it and does not create a server-side upload cache.

CSV preview is stateless. Confirmation requires the exact file again plus JSON-
encoded `CsvImportPlan` and `CsvImportConfirmation` form fields. The existing
confirmation service checks the document fingerprint before an atomic database
write, preserves every raw source row, quarantines invalid rows, and handles exact
and probable duplicates according to the reviewed plan.

PDF preview and review are also stateless. Review requires the exact PDF plus its
source path (`digital_pdf` or `ocr_pdf`), account, currency, and confidence
threshold. Confirmation requires those exact inputs again plus a JSON-encoded
`StatementApproval`. The server re-extracts the PDF and reconstructs the review on
both calls; it does not trust a caller to return an altered preview or review
object. The document hash and review contract then bind approval to that source.

PDF confirmation returns an `ApprovedStatement` but deliberately does not write
it to SQLite. Safe PDF persistence must later atomically store the import batch,
every original extracted row, rejected evidence, approved transactions, confirmed
coverage, and balance snapshots. Returning an approved in-memory result must not
be described as a completed import.

## Privacy and errors

The API returns verified transaction descriptions needed by the local product but
does not expose auditable raw-import payloads through transaction endpoints.
Readiness checks table names only and do not query financial rows. Normal logs and
error responses must not contain upload bytes, raw statement rows, merchant text,
transaction descriptions, request bodies, local paths, SQL details, or tracebacks.

Errors use the stable `ApiProblem` contract. Typical statuses are:

- `400` for a supported source whose contents cannot be processed;
- `404` for a missing local record;
- `409` when current state or explicit review prevents the operation;
- `413` for an oversized upload;
- `415` for an unsupported file or media type;
- `422` for an invalid request or multipart JSON contract;
- `503` when the local database is unavailable or not migrated; and
- `500` for an unexpected private internal failure.

Validation issues contain controlled locations, error types, and messages. They do
not echo the rejected field value or uploaded body. FastAPI debug tracebacks remain
disabled even when the wider application debug setting is enabled.

## Manual verification with synthetic data

Run the self-contained fictional workflow:

```bash
make demo-api
```

Expected output:

```text
CashFlow AI synthetic API check
health: ok
CSV preview rows: 2
verified transactions imported: 2
verified transactions returned: 2
raw source payload returned: false
temporary database retained: false
```

The demo creates the current schema in a temporary SQLite database, calls the HTTP
application in process, and removes the database afterward. To experiment,
change only the fictional display/account names or synthetic CSV dates, descriptions,
and amounts in `src/cashflow_ai/api/demo.py`. Keep
`opening balance + signed transaction total = closing balance`, and never insert
real financial information into a committed demo or test.

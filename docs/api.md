# Local HTTP API

Commits 30 and 31 expose the existing ingestion and decision-support services
through a local FastAPI application. The API is an adapter around application and
domain code: routes validate HTTP input, resolve dependencies, call a service, and
return a typed response. They do not reimplement parsing, reconciliation,
categorisation, analytics, forecasting, anomaly detection, or planning.

The Streamlit frontend calls these endpoints through its typed local client instead
of accessing the database or domain services directly. Profile/account onboarding
and statement preview/review are interactive. Transaction review and analytics are
interactive; recurrence, forecast, planning, scenario, anomaly-review, and model-
information controls are also interactive.
Forecast, planning, scenario, recurrence, and anomaly calculations are rebuilt
server-side from owned, cutoff-bound inputs; a caller cannot submit a fabricated
model result or balance path as trusted evidence.

The current normal navigation uses the newer statement-workspace routes described
below and deliberately gates the older profile/account analytics, forecast, and
planning screens. Those existing routes remain supported for regression tests and
developer demos; they are not loaded as an implicit fallback when the workspace UI
starts blank.

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
| `POST /api/v1/workspaces` | Create a blank saved or temporary GBP statement workspace | Draft memory only |
| `DELETE /api/v1/workspaces` | Delete every active and saved statement workspace after confirmation | All workspace data deletion |
| `GET /api/v1/workspaces/latest-saved` | Explicitly restore the latest finalized saved workspace | None |
| `GET /api/v1/workspaces/{workspace_id}` | Read the selected active or saved workspace | None |
| `POST /api/v1/workspaces/{workspace_id}/review` | Parse and combine several CSV/digital-PDF files | Draft memory only |
| `PATCH /api/v1/workspaces/{workspace_id}/rows` | Edit, include, or reject canonical candidate rows | Draft memory only |
| `DELETE /api/v1/workspaces/{workspace_id}/sources/{source_id}` | Remove one draft source and its rows | Draft memory only |
| `POST /api/v1/workspaces/{workspace_id}/finalize` | Approve the canonical table after every required confirmation | Canonical saved projection, or none in temporary mode |
| `GET /api/v1/workspaces/{workspace_id}/download` | Build finalized canonical CSV in memory | None |
| `DELETE /api/v1/workspaces/{workspace_id}` | Delete the selected workspace after explicit confirmation | Selected workspace deletion |
| `POST /api/v1/imports/csv/preview` | Validate and preview an uploaded CSV | None |
| `POST /api/v1/imports/csv/confirm` | Revalidate and atomically import an exact confirmed CSV | Confirmed import and retained source rows |
| `POST /api/v1/imports/pdf/text/preview` | Extract an embedded-text PDF for developer inspection | None |
| `POST /api/v1/imports/pdf/review` | Return ready, mapping-required, or unsupported-layout digital-PDF review state | None |
| `POST /api/v1/imports/pdf/confirm` | Re-extract and atomically persist an exact approved digital PDF | Confirmed import and retained source rows |
| `GET /api/v1/imports/{import_batch_id}/context` | Read stored coverage, balances, flags, and inert notes | None |
| `GET /api/v1/accounts/{account_id}/transactions` | List verified transactions | None |
| `POST /api/v1/transactions/search` | Search/filter profile-owned verified transactions | None |
| `GET /api/v1/transactions/{transaction_id}` | Read one verified transaction | None |
| `GET /api/v1/profiles/{profile_id}/duplicates/reviews` | List probable duplicate evidence | None |
| `POST /api/v1/profiles/{profile_id}/duplicates/{raw_transaction_id}/review` | Keep or reject a probable candidate | Review state; a kept candidate becomes verified |
| `POST /api/v1/analytics/cash-flow` | Calculate role- and coverage-aware cash flow | Derived freshness metadata only |
| `POST /api/v1/analytics/coverage` | Calculate the exact known, partial, and missing periods | Derived freshness metadata only |
| `POST /api/v1/coverage/freshness` | Assess transaction, balance, and statement age | None |
| `GET /api/v1/accounts/{account_id}/financial-revision` | Read the source-data revision | None |
| `GET /api/v1/accounts/{account_id}/derived-freshness` | Read current, stale, or unavailable derived states | None |
| `POST /api/v1/recurring/detect` | Refresh cutoff-safe recurring candidates | Candidates and freshness metadata |
| `GET /api/v1/profiles/{profile_id}/recurring` | List persisted candidates without rerunning detection | None |
| `POST /api/v1/recurring/reviews` | Confirm or cancel one recurring candidate | Explicit review state |
| `GET /api/v1/categories` | List the persisted category taxonomy | None |
| `GET /api/v1/profiles/{profile_id}/categorisation/reviews` | List low-confidence category decisions | None |
| `POST /api/v1/categorisation/feedback` | Apply an explicit category correction | Correction and optional requested rule |
| `POST /api/v1/financial-roles/suggestions` | Generate advisory transfer/refund/reimbursement suggestions | Suggestions only |
| `GET /api/v1/profiles/{profile_id}/financial-roles/reviews` | List financial-role reviews | None |
| `POST /api/v1/financial-role-suggestions/{suggestion_id}/confirm` | Confirm a suggestion | Role audit and invalidation metadata |
| `POST /api/v1/financial-role-suggestions/{suggestion_id}/reject` | Reject a suggestion | Review state only |
| `POST /api/v1/transactions/{transaction_id}/financial-role` | Apply an explicit financial-role action | Role audit and invalidation metadata |
| `GET /api/v1/transactions/{transaction_id}/financial-role-audits` | List immutable financial-role history | None |
| `POST /api/v1/forecasts/evaluate` | Chronologically compare the candidate and baselines | Freshness metadata only |
| `POST /api/v1/forecasts/balance` | Build a daily balance path with empirical intervals | Freshness metadata only |
| `POST /api/v1/budgets` | Create an explicit budget | Budget |
| `GET /api/v1/profiles/{profile_id}/budgets` | List budgets active on a supplied date | None |
| `POST /api/v1/goals` | Create a savings or minimum-balance goal | Goal |
| `GET /api/v1/profiles/{profile_id}/goals` | List owned goals | None |
| `POST /api/v1/planning/evaluate` | Evaluate budgets, goals, and safe weekly spending | Freshness metadata only |
| `POST /api/v1/scenarios/evaluate` | Compare an isolated hypothetical scenario | Freshness metadata only |
| `POST /api/v1/anomalies/detect` | Identify review signals without alleging fraud | Freshness metadata only |
| `POST /api/v1/anomalies/feedback` | Recompute and review one current anomaly suggestion | Controlled review state |
| `GET /api/v1/models` | List data-minimised model metadata | None |
| `GET /api/v1/models/{task}/active` | Read an explicitly active eligible model | None |

Every collection response uses `{items, limit, offset, total}`. `limit` defaults
to 50 and is restricted to 1–100; `offset` defaults to zero. Ordering remains
owned by the underlying service so pages are repeatable for unchanged local data.

## Statement-workspace contracts

The workspace routes are the normal UI boundary; they do not infer a profile or
account from the legacy database. `POST /api/v1/workspaces` creates an isolated GBP
draft with `saved` or `temporary` retention. The client may explicitly ask for the
latest saved workspace, but ordinary startup never performs that request.

Review accepts 1–20 multipart files and may mix CSV with selectable-text digital PDF.
All files are required by product policy to describe the same personal current or
savings account. Each upload is parsed separately. Ambiguous sources return bounded
mapping columns/sample rows; the client must re-submit the exact file with a mapping
bound to its hash and, for PDF, its structure digest. Unknown, scanned, encrypted, or
unsafe layouts are represented as unsupported and can be removed without discarding
other draft sources.

The server combines usable candidates, removes only deterministic exact duplicates,
and flags probable duplicates for a keep-or-reject decision. `PATCH .../rows` applies
an optimistic workspace revision and a per-row expected revision so a stale browser
cannot overwrite newer review state. Confirmed rows require a date, description,
non-zero penny-precise signed amount, category, and sign-compatible financial role.

Finalization is rejected until every row is included or excluded and every probable
duplicate has a decision. The request must explicitly confirm source review, day/month
date interpretation, positive-in/negative-out signs, statement coverage and gaps,
and any latest balance evidence. Temporary mode keeps the finalized table only in the
API process store. Saved mode writes only the included canonical rows, confirmed
coverage, and optional balance; it never writes upload bytes, extracted PDF text,
source names or hashes, page/row provenance, mapping samples, or rejected rows.

The identifier-specific delete endpoint removes exactly the named workspace. The
collection delete endpoint removes every active and saved statement workspace. Both
require a request body whose `confirmed` field is literally `true`. Neither operation
deletes legacy imports, models, downloaded exports, backups, or copied databases. API
shutdown clears the process-memory store. Browser/tab close alone does not guarantee a
server request, so it is not a temporary-workspace deletion contract. Analytics,
forecast, and planning workspace endpoints are deliberately absent from this
checkpoint; their legacy database-backed API routes remain available for regression
use.

All `/api/v1/workspaces` responses include `Cache-Control: no-store`. The local API
rejects non-loopback/unrecognised Host headers to prevent DNS-rebinding access. A
request-body ceiling applies before FastAPI parses workspace JSON or multipart data;
review still enforces 1–20 files, a 50 MiB aggregate source limit, existing per-format
limits, and a 64 KiB mapping-JSON limit. Parsing runs in a worker thread so a bounded
PDF does not block the API event loop. Canonical CSV text fields that could be treated
as spreadsheet formulas are emitted as literal text.

## Upload and confirmation contracts

Uploads use `multipart/form-data` and are read into bounded memory. CSV and PDF
limits remain owned by the existing import modules. The API closes each uploaded
file after reading it and does not create a server-side upload cache.

CSV preview is stateless. Confirmation requires the exact file again plus JSON-
encoded `CsvImportPlan` and `CsvImportConfirmation` form fields. The existing
confirmation service checks the document fingerprint before an atomic database
write, preserves every raw source row, quarantines invalid rows, and handles exact
and probable duplicates according to the reviewed plan.

Probable rows retain a versioned canonical candidate snapshot separately from their
unchanged raw payload. This is the minimum validated evidence needed for an explicit
**keep as separate transaction** decision. Older rows created before that additive
column remain reviewable and rejectable, but keeping them requires a fresh import
rather than reconstructing financial values from arbitrary raw columns. The review
endpoint scopes profile and account ownership, uses server receipt time for durable
audit/invalidation, and marks an import verified only after its last needs-review row
is resolved.

Digital-PDF review is also stateless. It requires the exact PDF plus account and
currency. A safely recognised statement returns `ready`; a stable table with
ambiguous headings returns `mapping_required` with at most 20 sample rows; an unsafe,
image-only, or unsupported layout returns `unsupported_layout` and recommends CSV.
Mapping JSON contains the exact file hash and reconstructed structure digest plus the
user's date, description, amount/debit-credit, and optional balance selections.

Confirmation requires the exact inputs again plus JSON-encoded `StatementApproval`
and any mapping. The server re-extracts the PDF, reconstructs the review, applies only
explicit decisions, and then re-extracts again inside the atomic persistence trust
boundary. It stores the import batch, every accepted/rejected raw row, unique verified
transactions, duplicate outcomes, confirmed coverage, balance snapshots, and
invalidation together. It returns `PdfImportSummary`, not client-supplied evidence.

The local OCR status, preview, review, and in-memory confirmation endpoints remain
available for developer regression tests but use `include_in_schema=False`. They are
not part of the normal Version 1 interface or its OpenAPI support surface.

## Decision-support contracts

Analytics accepts an owned account scope, inclusive date range, and account or
consolidated view. Missing statement dates remain unknown rather than being turned
into zero spending. Categories and financial roles remain separate, so a transfer
can still describe what it concerned without being counted as income or expense.

Forecast evaluation accepts a dataset plan and model-selection policy. Balance
forecasting additionally accepts a path policy, but the dataset and path must share
one profile, one account, and one knowledge cutoff. Planning requires exactly one
ordered server-built forecast request per selected account. Scenario, forecast, and
planning scopes must identify the same profile and account. A knowledge cutoff in
the future is rejected at the API boundary.

Derived calculations capture each account's source revision before work and
atomically mark the participating outputs current only if every revision still
exists after work. A later
confirmed import, category/role correction, balance change, or recurring review can
mark affected output types stale. The API stores this revision/freshness metadata,
not private analytics, forecast, anomaly, planning, or scenario response payloads.

Anomaly responses are review aids, not fraud allegations. Feedback requests repeat
the bounded detection plan; the server recomputes the alert before saving only its
controlled status, score, and signal-code reasons. Feedback does not alter the
transaction or retrain a model. Forecast intervals are empirical estimates, not
guarantees. Scenario responses are hypothetical and are not persisted as actual
transactions, budgets, goals, or forecasts.

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

Controlled domain errors also use these rules: absent records or unavailable
evidence return `404`; stale, conflicting, duplicate, already-reviewed, or
ineligible state returns `409`; and malformed stored metadata returns `500` without
exposing that metadata.

Validation issues contain controlled locations, error types, and messages. They do
not echo the rejected field value or uploaded body. FastAPI debug tracebacks remain
disabled even when the wider application debug setting is enabled.

## Manual verification with synthetic data

Run the isolated workspace walkthrough:

```bash
make demo-workspace
```

Expected output includes `mixed sources accepted: 2`, `combined canonical rows: 3`,
`saved rows restored: 3`, and `no` for original bytes persistence, temporary SQLite
persistence, and temporary memory after simulated API shutdown. All source data is
fictional and created in memory.

The older full API demonstration remains available:

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
role-aware cash flow: income=1000.00 expenses=400.00 net=600.00
coverage status: complete
analytics freshness: current
pagination: returned=1 total=2
raw source payload returned: false
temporary database retained: false
```

The demo creates the current schema in a temporary SQLite database, imports two
fictional rows, explicitly assigns their financial roles, calculates cash flow,
checks coverage/freshness, exercises pagination, and removes the database afterward.
To experiment, change only the fictional display/account names or synthetic CSV
dates, descriptions, and amounts in `src/cashflow_ai/api/demo.py`. Keep
`opening balance + signed transaction total = closing balance`, and never insert
real financial information into a committed demo or test.

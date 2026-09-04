# Local Streamlit frontend

The frontend is a loopback-only Streamlit client over the existing FastAPI
boundary. It now provides:

- a home page with local API/database readiness and privacy guidance;
- first-run profile setup using display name, currency, and IANA timezone;
- current/checking and savings account setup without bank credentials or account
  numbers;
- CSV preview, column mapping, statement context, explicit confirmation, and an
  atomic import result;
- digital-PDF extraction and local scanned/camera-PDF OCR review;
- targeted correction or rejection of uncertain PDF rows, balance and coverage
  confirmation, reconciliation warnings, and final statement approval; and
- a transaction workspace with local search, account/date/category/role filters,
  explicit category and financial-role corrections, transfer/refund/reimbursement
  suggestions, and probable-duplicate decisions;
- coverage and freshness indicators plus observed income, expense, savings,
  category, cadence, and gap-preserving balance charts; and
- recurring-series detection with explicit confirm/reject controls;
- on-demand balance forecasts with a selectable horizon, expected path,
  uncertainty band, confirmed upcoming flows, balance source, cutoff, model, and
  low-data/stale-data warnings;
- budget and financial-goal setup, progress, and a conservative safe-weekly-spending
  estimate;
- temporary baseline-versus-scenario comparisons for all supported scenario types;
- an explainable anomaly-review queue with explicit expected/unusual feedback; and
- aggregate model versions, evaluation metrics, eligibility, and active status.

The import page does not reproduce parsing or financial logic. Typed form values go
through the local API client to the existing backend contracts.

## Local and privacy boundary

Both processes are restricted to loopback. `CASHFLOW_API_HOST` and
`CASHFLOW_UI_HOST` accept only `127.0.0.1`, `localhost`, or `::1`; ports accept
1–65535. The frontend client accepts only explicit local HTTP URLs and relative API
paths, does not inherit proxy settings, and applies longer bounded timeouts only to
document extraction calls. Loopback is not authentication: do not expose either
process to another machine.

Streamlit application session state contains only page, profile, and account
identifiers plus a display preference. The application does not copy upload bytes,
transaction text, amounts, balances, API responses, or forecasts into that state or
an application-managed file cache. Streamlit's upload widget supplies the current
file to a request; stateless confirmation deliberately sends the exact bytes again
so the backend can re-extract and verify them.

Transaction searches and dashboard results are requested again on each Streamlit
rerun; they are not copied into the application-managed session model. Descriptions
and amounts are necessarily visible in the local workspace, but raw import payloads
are not returned by its transaction endpoints. Chart conversion from `Decimal` to
floating point is a presentation-only copy; stored and API money remains fixed
precision.

Recurring candidates and forecasts are also requested again rather than stored in
session state. The browser supplies identifiers, dates, horizon, and explicit review
actions; the API rebuilds all evidence and models locally. Confirming or rejecting a
series changes derived-data state through the existing backend invalidation rules.

Budgets and goals are stored only after an explicit save action. Scenario definitions
and comparison paths remain temporary and are requested again on each rerun. Anomaly
feedback sends the original bounded scan plan plus one transaction identifier; the
backend recomputes the suggestion before saving only its controlled review status,
score, and signal-code reasons. It does not store descriptions, alter transactions,
or retrain the anomaly model. Model evaluation views contain aggregate registry
metadata and never expose learned vocabulary or transaction-level predictions.

CSV confirmation persists an import atomically through the established service. PDF
approval remains an in-memory result because atomic PDF persistence has not been
implemented. The page states this limitation after approval and never calls it a
saved import. Free-text statement notes are reference-only metadata and do not alter
categories, roles, analytics, or forecasts.

## Manual verification with fictional data

Prepare the local environment and reproducible synthetic files:

```bash
make setup
make db-upgrade
make demo-data
make demo-statements
```

Start the API in one terminal:

```bash
make api
```

Start the UI in a second terminal and open `http://127.0.0.1:8501`:

```bash
make ui
```

Expected first-run and account behaviour:

1. **Import statements** asks for a local profile if none exists. Use `Fictional
   User`, `GBP`, and `Europe/London`.
2. Add an account named `Fictional Current`, choose `Current`, and leave
   the optional institution label blank.
3. The account becomes the selected import destination. No login, bank password, or
   account number is requested.

For the CSV workflow, choose **CSV**, upload
`data/demo/generated/student/student_canonical.csv`, and check the proposed
mapping. Use a complete statement period from `2024-01-01` to `2025-12-31`, leave
reported balances disabled, tick the exact-file confirmation, and submit. Expected
behaviour is a preserved preview followed by an import summary that separately
counts imported, exact-duplicate, probable-duplicate, and rejected rows. Repeating
the same file reports a repeated file rather than adding a second import.

Open **Transactions & analytics** after that import. Under the transaction table:

1. Search for a fictional merchant such as `RENT`, then clear it and vary the
   account, category, financial-role, and date filters. Expected: only matching
   verified rows appear; raw source payloads never appear.
2. Choose one row, change its category to another visible category, and save. Then
   set its financial role to `expense` or `income` as appropriate and save. Expected:
   both changes survive refresh, the role change has an audit trail in the backend,
   and the preserved raw import row is unchanged.
3. In **Review suggestions**, refresh role suggestions. Confirm or reject only a
   clearly fictional transfer/refund/reimbursement. Expected: scanning alone changes
   no role; only confirmation applies the suggested role.
4. Review a probable duplicate from the generated data when one is listed. Choose
   **Keep as a separate transaction** only when the dates represent two real
   fictional purchases, or **Reject as duplicate** otherwise. Expected: the queue
   removes the decision while the source row stays preserved. A legacy row without a
   retained candidate can be rejected but must be re-imported before it can be kept.
5. In **Dashboard**, select the imported account and its exact statement dates.
   Expected: a coverage timeline labels known and missing dates; freshness reports
   `active forecasting` only when every displayed policy passes; missing dates break
   balance lines; headline values say `Observed` unless coverage is complete.

Open **Forecast & planning** for the Commit 36 workflow:

1. Set **Recurring evidence date** to the last completed UTC date covered by the
   fictional import, then select **Refresh recurring patterns**.
   Expected: repeated fictional merchants are listed with frequency, expected amount,
   next-payment date, confidence, evidence count, and current review status.
2. Confirm or reject one pending fictional pattern. Expected: the explicit status is
   reported; detection alone never silently confirms it.
3. Open **Forecast**, select the fictional account, use the same cutoff date, choose
   `14`, `30`, `60`, or `90` days, choose the fictional profile's usual income days,
   then select **Generate forecast**. Expected: either
   a controlled insufficient-evidence message or a chart whose line is the expected
   balance and whose shaded area is its empirical uncertainty range. The result also
   names the verified opening-balance source, cutoff, selected model and best
   baseline, selection reason, held-out errors when available, final range, upcoming
   confirmed flows, and any low-data/stale-data warning.
4. Open **Budgets & goals**. Save a fictional monthly category budget and a weekly
   discretionary budget, then save a fictional savings target and minimum-balance
   goal. Select the relevant fictional accounts and **Evaluate plan**. Expected:
   coverage-aware progress, controlled unavailable values where dates are missing,
   goal requirements, warnings, and an estimated safe weekly amount. The page must
   call it an estimate and not financial advice.
5. Open **Scenarios**, select a fictional one-off purchase of `100.00`, choose a date
   inside the displayed horizon, and select **Compare scenario**. Expected: separate
   baseline and scenario lines plus ending-balance and safe-spending differences; the
   page states that no transaction or plan changed. Safely vary the positive amount,
   horizon, supported type, frequency, and synthetic date. Cancelling a subscription
   requires an explicitly confirmed recurring candidate.
6. Open **Anomaly review** and enable **Run anomaly scan**. Expected: either explicit
   low-history/coverage warnings and rule-only results, or carefully worded review
   suggestions with controlled reasons. Choose **This was expected** or **Keep as
   unusual** on one fictional item. Expected: the saved review appears on the next
   scan, the source transaction remains unchanged, and the UI never calls it fraud.
7. Open **Model evaluation** and vary the task filter. Expected: aggregate model
   version, training dates, activation eligibility/status, and metrics when local
   registrations exist; an empty state is valid when none have been registered.

Safe parameters to vary are the four displayed horizons and dates inside synthetic
verified coverage. A year of contiguous weekly evidence is the intended full model
check; shorter data should fail safely or use a baseline, not claim false certainty.
Today is deliberately unavailable because recurrence and forecast evidence requires
a fully completed UTC calendar day; the default is yesterday in UTC.
Income days are explicit because payday distance is a model feature; selecting a day
does not invent or schedule income, and an empty selection cannot run a forecast.

Safe values to vary are the fictional search text, category/role choice, dashboard
accounts and statement-contained dates. Corrections are real local database writes,
so regenerate a disposable local database if you want to repeat the test from a
known state.

For the digital-PDF workflow, choose **Digital PDF** and upload
`data/demo/generated/statements/fictional_digital_statement.pdf`. Expected values
are a period of August 2026, opening balance `1000.00`, closing balance `1600.00`,
two fictional rows, and reconciled arithmetic. Complete every displayed evidence
confirmation and approve. The result must say that two rows were approved **in
memory** and were **not saved**.

For the OCR path, first check the local dependency:

```bash
make check-ocr
```

If available, choose **Scanned / camera PDF** and upload
`data/demo/generated/statements/fictional_scanned_statement.pdf`. Extraction must
show visible progress and confidence-based review controls. Confirm or reject every
targeted row rather than assuming OCR is correct. OCR output varies by installed
Tesseract version, so the safe expected outcome is either a reviewable preview or a
controlled extraction warning—never silent persistence. If Tesseract is absent, the
page explains that local OCR is unavailable while CSV/digital PDF remain usable.

Safe parameters to vary are the fictional names, the OCR review threshold, and the
generated source choice. Do not use real data in screenshots, test fixtures, or bug
reports.

## Current limitations

- Approved PDF rows are not yet persisted; the original file must be retained and
  re-reviewed after that later boundary is implemented.
- Bank PDF layouts are not standardised. Digital extraction and OCR support the
  tested conservative layouts, not every institution or scan quality.
- Budget/goal update and deletion, saved scenarios, and pagination controls are not
  implemented. Transaction, recurring, and planning searches currently return at
  most the first 100 matches.
- Forecast intervals are empirical estimates rather than guarantees, and recursive
  errors can compound at longer horizons.
- The client is synchronous; spinners make bounded extraction work visible, but
  background jobs and cancellation are not implemented.
- Accessibility, browser compatibility, authentication, and deployment hardening
  remain later work.

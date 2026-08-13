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
embedded-text and scanned-PDF extraction, plus statement reconciliation and
explicit PDF review contracts, verified balance snapshots, and a conservative
financial-data freshness assessment, plus user-confirmed financial-role
suggestions for transfers, refunds, and reimbursements, and deterministic
coverage-aware cash-flow analytics, and explainable deterministic transaction
categorisation, plus a leakage-aware TF-IDF and Logistic Regression transaction
category training and evaluation pipeline**. PDF persistence, APIs, user
interfaces, forecast generation, hybrid category assignment, and other
machine-learning components have not been implemented yet.

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
  stored with the import. Accepted unique rows with a running balance also
  create dated balance snapshots, and an unexpected failure rolls everything
  back.
- Digital PDFs downloaded from online banking can now be validated and parsed
  in memory using embedded text, recognised tables, or a conservative generic
  fallback. Candidates retain their source page and require review; no PDF row
  is persisted yet.
- Camera-captured or scanned PDFs can now be rendered and processed with local
  Tesseract OCR. Raw recognised lines, page and line confidence, rotation, and
  preprocessing metadata are retained for review.
- Every PDF produces a reviewable draft. The user must confirm the recognised
  dates, descriptions, amounts, and balances before transactions are imported.
  The shared review service calculates opening balance plus signed transactions
  against the closing balance, targets extraction errors and low-confidence OCR
  fields, and requires explicit date/sign confirmations where the source is
  ambiguous. A confirmed date format reparses ambiguous transaction and posting
  dates unless the user supplied a corrected date. Corrections cannot move a row
  to another account or currency or assign its category or financial role, and
  both approved and rejected rows keep their complete original extraction
  lineage.
- Opening and closing balance evidence retains the raw amount text, exact PDF
  hash and source adapter, page and line, extraction provenance, and OCR
  confidence where applicable. Confirmed or corrected balances remain in the
  approved result even when a missing balance endpoint makes reconciliation
  unavailable. The statement period is also confirmed or corrected and retained,
  so a later balance snapshot can use the actual statement boundary instead of a
  guessed last-transaction date.
- All three input paths converge on the same canonical transaction contracts;
  PDFs are not trusted merely because they can be converted to tabular text.

### Implemented balance and freshness boundary

Balance evidence remains separate from cash-flow activity. Explicitly confirmed
CSV statement context creates opening and closing snapshots at the coverage
boundaries, accepted unique CSV rows can create running-balance snapshots, and a
manually entered current balance creates a verified snapshot without creating a
transaction, import, or statement coverage.

For a requested assessment date, the service selects the newest verified,
non-future balance. If multiple observations share that date, the preference is
manual, statement closing, running balance, then statement opening, followed by
recording time and database identity. It calculates separate transaction,
balance, and coverage ages, plus `data_freshness_days`, the age of the most
recent trusted transaction-or-balance evidence. The caller must explicitly set
the maximum permitted age for each evidence type and the minimum required
consecutive coverage; there are no hidden product thresholds.

Only verified `complete`/`overlapping` coverage and known portions of verified
`gapped` coverage can prove continuity. `partial` or `unknown` coverage, missing
evidence, and future or unverified records cannot. The result is either
`active_forecasting` or `archive`, with stable warnings explaining the decision.
This is a data-readiness gate only: it does not run a forecast, expose an API, or
provide a user interface.

### Implemented financial-role review boundary

Verified transactions retain a financial role independently from their future
spending category. Deterministic rules can suggest matched transfers between the
local user's accounts, one-sided possible transfers, explicit refunds, and
explicit reimbursements. Suggestions are advisory: generating or displaying one
does not change the transaction.

A user can confirm or reject a suggestion, directly choose income, expense,
transfer, refund, reimbursement, cash withdrawal or exclusion, or add a
structured `needs_review` flag. Paired transfer confirmation changes both legs
atomically and records one immutable audit entry per changed transaction. Role
signs are enforced, competing suggestions are rejected after a decision, and
raw source values and categories remain untouched.

Statement flags and free-text notes appear only as reference context in the
review queue. They are never parsed to assign a role. There is no API or visual
review screen yet, and the role-review stage itself does not calculate totals.

### Implemented coverage-aware analytics boundary

The read-only analytics service calculates role-aware income, expenses, refunds,
reimbursements, cash withdrawals, external net cash flow, transfer movement,
savings rate, expense-category totals, largest transactions, monthly summaries,
and gap-preserving balance history. All money remains fixed-precision `Decimal`.

Results are tied to explicit verified statement coverage. A fully covered empty
month is genuinely zero; a month with no trusted coverage is unavailable rather
than zero. Incomplete periods are labelled `observed_only`, and savings rate is
withheld when coverage is incomplete, a financial role remains unknown, or
income is zero. Account views show transfer movement; consolidated views suppress
both legs of a currently valid confirmed internal-transfer pair.

Expense spending is currently `unclassified` for recurrence because recurrence
detection belongs to a later stage. Null categories remain an explicit
uncategorised bucket for transactions that have not run through categorisation.
The service writes nothing, stores no derived report, and exposes no API or UI
yet.

### Implemented deterministic categorisation boundary

Verified transactions can now be assigned Version 1 categories such as housing,
groceries, utilities, transport, subscriptions, health, education, and travel.
The service follows a visible precedence: the latest transaction-specific user
decision, then an active scoped personal rule, an exact known-merchant mapping,
a whole-phrase keyword rule, and finally `needs_review`. The separately trained
Commit 19 model candidate does not change that sequence. Commit 20 will insert
eligible predictions between keyword rules and the fallback. The deterministic
service itself does not make probabilistic predictions.

Personal rules can be restricted by merchant, direction, account, description
phrase, and an inclusive absolute-amount range. All supplied restrictions must
match. Rule text is normalised for case and punctuation without changing the
stored merchant or description, and ambiguous equally ranked rules are sent to
`needs_review` instead of choosing by input order.

Each run returns a privacy-safe explanation containing a controlled reason,
precedence source, rule identity where applicable, and which fields matched. It
updates only the verified transaction's category; financial role, raw import
evidence, source text, amounts, dates, statement notes, and flags remain
unchanged. Personal-rule storage, a correction workflow, APIs, and review screens
belong to Commit 20 and later interface stages.

### Implemented ML categoriser candidate boundary

The standalone category-model pipeline builds a supervised dataset only from
owned, verified transactions with an explicit category correction available by
the requested knowledge cutoff. It excludes unresolved or ignored financial
roles, review-pending transfer candidates, duplicate evidence, `needs_review`
labels, and PDF/OCR rows whose document has not been fully verified. Current
category values and corrections created after a historical cutoff are not used
as training labels. Financial roles are likewise reconstructed from the latest
role-change audit available at that cutoff; without an audit, a transaction is
treated as `unknown` regardless of its current stored role.

Each model combines word and character TF-IDF features over temporary normalised
merchant-and-description text with Logistic Regression. Its preprocessing is fit
from scratch inside each evaluation split. Evaluation includes an unshuffled
chronological holdout, a merchant-group holdout whose test merchants are absent
from training, and a most-frequent-category baseline on the same rows. Both
holdouts report macro and weighted F1, precision, recall, per-class results, and
a confusion matrix.

A successful run can save a candidate model and a metadata sidecar under a
private ignored local model directory. Metadata records the taxonomy and feature
schema, cutoffs, split policy, parameters, aggregate class counts, baseline and
model metrics, separate historical and final dataset exclusion counts, selection
result, software versions, and an artefact checksum; it does not contain training
descriptions or merchant names. The learned TF-IDF
vocabulary remains private inside the artefact and only trusted, locally created
artefacts may be loaded.

This stage neither changes a transaction category nor inserts ML into the
deterministic precedence. Commit 20 owns hybrid rule-plus-model inference,
confidence thresholds, low-confidence review, and feedback. Commit 26 owns the
database model registry and active-model lifecycle.

The CSV preview, confirmation, and persistence pipeline currently consists of
Python services rather than an upload or review screen.
Text-based and scanned-PDF preview extraction are Python services. The shared
PDF correction and confirmation boundary is also a Python service; a later
stage will connect its approved output to persistence and a user interface. It
does not currently write an approved or rejected PDF row to the database.

## Development setup

Prerequisites:

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Tesseract OCR on `PATH` for scanned or camera-captured PDF extraction

On macOS, install the local OCR executable with:

```bash
brew install tesseract
```

Tesseract is not needed for CSV or embedded-text PDF imports. If it is missing,
the OCR adapter returns a specific setup error rather than sending the document
to an external service. Verify the local executable after installation with:

```bash
make check-ocr
```

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
- [`docs/analytics.md`](docs/analytics.md)
- [`docs/categorisation.md`](docs/categorisation.md)
- [`docs/modelling.md`](docs/modelling.md)
- [`docs/evaluation.md`](docs/evaluation.md)
- [`docs/privacy.md`](docs/privacy.md)

## Status and roadmap

The planned implementation is deliberately incremental. The project foundation,
quality tooling, typed settings, structured logging, privacy-safe synthetic
data, canonical data contracts, CSV preview/mapping, transaction cleaning,
duplicate/statement-overlap detection, and SQLite persistence are configured.
Confirmed CSV imports, text-PDF extraction, and local scanned-PDF OCR are
implemented. Statement balance reconciliation and targeted PDF review are also
implemented. Verified balance tracking and financial-data freshness assessment
are implemented as Python service boundaries. Conservative financial-role
suggestions, explicit user decisions, and role-change audit history are also
implemented. Coverage-aware cash-flow analytics and gap-preserving balance
history are implemented as read-only Python services. Deterministic merchant,
keyword, and caller-supplied scoped personal categorisation rules are implemented
with an explicit `needs_review` fallback. A standalone, evaluated ML category
candidate can now be trained and stored locally without changing transaction
categories. The next stages will add the hybrid correction workflow, APIs, the
frontend, deployment, and release documentation.

No feature listed here should be considered available until its implementation
and evaluation are present in the repository.

## Disclaimer

> CashFlow AI provides estimates based on historical and user-supplied
> information. Forecasts may be inaccurate and are not financial advice.

## Licence

This project is licensed under the [MIT License](LICENSE).

## Hybrid categorisation status

Commit 20 connects deterministic rules and the evaluated local classifier. User
decisions and personal, merchant, and keyword rules win first. ML handles only
unmatched rows: predictions meeting an explicit confidence threshold may be
applied; lower-confidence predictions leave the transaction unchanged and enter a
review queue. Decision source and ML model version are audited. Corrections are
transaction-only unless the user explicitly supplies and requests a narrow personal
rule. Corrected examples can be prepared for manual, cutoff-safe retraining; no
background retraining occurs.

## Recurring-payment status

Commit 21 detects weekly, fortnightly, monthly, quarterly, and annual patterns from
verified transactions using merchant, amount, and interval consistency. It predicts
the next date and confidence. Only expected dates inside verified statement coverage
count as missed; gaps remain unknown. Users explicitly confirm or cancel candidates,
and only confirmed expense members enter recurring-spend analytics.

## Forecast-data status

Commit 22 builds a daily coverage calendar, fully covered weekly discretionary
targets, past-only lag/rolling/payday/month features, confirmed recurring-flow
inputs, expanding-window folds, and a final chronological test. Unknown dates stay
null and break lag chains. Five baselines establish what later ML must beat. Run
`make demo-forecast` for a readable synthetic check.

# Local model registry

Commit 26 adds a lightweight SQLite registry for the models already evaluated by
the categorisation, cash-flow forecasting, and anomaly modules. It answers four
questions locally: which versions exist, what evidence and configuration produced
each version, whether a version passed its model-specific gate, and which eligible
version is explicitly selected for each modelling task.

The registry is an audit and selection boundary. It does not train, load, deploy,
schedule, or automatically replace a model. There is no MLflow server, cloud model
store, staging/production workflow, or automated retraining.

## Recorded metadata

Each version has an immutable model name and version plus:

- modelling task and model type;
- training start and end dates;
- feature-schema version and controlled feature names;
- category-taxonomy version when applicable;
- aggregate evaluation metrics with their slice and unit;
- controlled reproducibility parameters;
- a private, repository-relative artefact path when an artefact exists;
- creation time and metadata-format version; and
- activation eligibility, current active state, and activation time.

Adapters deliberately reduce existing model results before registration. The
categorisation adapter retains aggregate holdout and baseline scores but not
per-category confusion matrices or learned vocabulary. The forecasting adapter
retains aggregate candidate and baseline error metrics but not held-out actuals or
predictions. The anomaly adapter retains run counts, minimum coverage, parameters,
and feature schema but not transaction-level alerts. Its unsupervised run is recorded
as ineligible for activation because Commit 25 has no labelled anomaly benchmark or
persisted estimator artefact.

Artefact paths must be relative and inside `models/`; absolute paths could disclose a
local username or filesystem layout. Model artefacts remain ignored private files.

## Explicit activation

Registration never activates a version. `activate_model` accepts an exact task and
database model ID, rejects versions that failed their model-specific gate, and
atomically replaces the previous active version. A partial unique SQLite index
enforces at most one active version per task even if application checks fail.
Repeated activation of the already-active version is idempotent.

“Active” means selected for a future caller of that modelling task. It does not mean
deployed to a server or automatically invoked by the current application. The API
and UI stages will later resolve the active registry record before using a model.

Migration `0007` extends the original `model_metadata` table without deleting its
existing fields. Old rows are retained as `legacy-0`, inactive, and ineligible;
missing evidence is never invented and old metadata is never silently trusted. The
downgrade removes only Commit 26 fields and preserves the original row values.

## Manual verification

Run the deterministic in-memory demo:

```bash
make demo-model-registry
```

Expected key output:

```text
registered versions: 2
active task: cash_flow_forecasting
active version: synthetic-2
previous active version replaced: true
transaction-level financial data stored: false
```

To leave the first fictional version active instead:

```bash
uv run cashflow-model-registry-demo --activate synthetic-1
```

The active version should become `synthetic-1` and replacement should be `false`.
Both commands use an in-memory database and fixed fictional metrics; they do not read
the configured database, bank statements, uploads, or local model artefacts.

## Current limitations

- Activation is a local metadata pointer; model loading is still owned by each model
  module.
- Forecast and anomaly estimators are currently in-memory candidates, so no artefact
  path is claimed for them.
- Anomaly metrics remain run diagnostics rather than supervised accuracy evidence.
- Registry rows are local database data and are not an enterprise experiment tracker.
- Read-only model-information API routes and an aggregate Commit 36 UI are available.
  Automated retraining, activation controls, and deployment remain later work.

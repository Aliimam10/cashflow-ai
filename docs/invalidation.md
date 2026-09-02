# Derived-data invalidation and recomputation

Commit 29 prevents an older calculation from being presented as current after its
financial source evidence changes. It stores revision and freshness metadata only;
analytics reports, forecasts, scenarios, model outputs, and their monetary payloads
remain transient and local.

## Source revisions and statuses

Every recorded source change increments one monotonic revision for its account. The
revision records only the account identity, integer revision, controlled change type,
and server-controlled timestamp.

Each governed output family has a separate required revision because not every change
affects every calculation. Its status is:

- `unavailable`: no result has been successfully computed for the required revision;
- `current`: the computed revision exactly equals the required revision; or
- `stale`: an earlier computed result exists, but a dependent source change raised the
  required revision.

The governed families are analytics, recurring series, anomaly alerts, budgets,
forecasts, scenarios, and model-performance comparisons.

## Dependency matrix

| Source change | Outputs invalidated |
| --- | --- |
| OCR correction | All governed outputs |
| Transaction-amount change | All governed outputs |
| Financial-role change | All governed outputs |
| Confirmed transfer | All governed outputs |
| Added statement | All governed outputs |
| Deleted import | All governed outputs |
| Category change | Analytics, anomaly alerts, budgets, scenarios, model comparisons |
| Current-balance change | Analytics, budgets, forecasts, scenarios |

Category does not enter recurrence detection or the aggregate cash-flow forecasting
features, so those two outputs remain current after a category-only correction. A
balance does not change transaction classification, recurrence, anomaly features, or
model evaluation, so those results remain current after a balance-only update.

## Atomic mutation hooks

Confirmed CSV import, category feedback, financial-role/transfer confirmation, and
manual balance entry call invalidation inside their existing database transaction.
If either the source write or freshness update fails, the whole unit of work rolls
back. Re-uploading the exact same statement does not create another revision because
the confirmed-import service returns its existing idempotent summary before the
invalidation boundary.

OCR correction, transaction-amount editing, and import deletion do not yet have
persistence services. Their controlled change types are available through
`record_source_data_change` so the Commit 30 APIs can call the same boundary in the
transaction that eventually owns those mutations. This commit does not invent an
unsafe delete or edit operation.

## Race-safe recomputation

`begin_derived_computation` captures the selected output's required revision before
work starts. The caller computes the private payload in memory. Only
`complete_derived_computation` can mark it current, and only when the stored required
revision still equals the token. A relevant change during computation produces
`source_changed_during_recomputation`; the old result remains stale.

`recompute_derived_result` provides the same lifecycle around a synchronous callback.
The callback's payload is returned but never written to freshness tables. A callback
exception leaves its previous status unchanged. `require_current_derived_result`
fails with `result_not_current` rather than allowing an unavailable or stale result
to be displayed.

## Persistence

Migration `0009` adds `financial_data_revisions` and `derived_result_states`. It does
not backfill guessed provenance, rewrite financial rows, or seed synthetic evidence.
Existing outputs therefore cannot be labelled current until explicitly recomputed.
Downgrade removes only freshness metadata and preserves accounts, imports,
transactions, balances, budgets, goals, and models.

## Manual verification

Run the fictional lifecycle:

```bash
make demo-invalidation
```

Expected output includes:

```text
initial analytics status: unavailable
statement revision: 1
analytics after recompute: current
analytics after category change: stale
forecast after category change: current
analytics after refresh: current
mid-computation change rejected: source_changed_during_recomputation
derived payload persisted: false
```

The demo uses one in-memory account and fictional dictionary payloads. It does not
create a database file or import a statement.

## Current limitations

- Recalculation is synchronous and caller-triggered; no background job queue exists.
- The future API must wrap each newly calculated result in the recomputation boundary
  before the UI may treat it as current.
- No import-delete, transaction-amount-edit, or OCR-correction persistence operation
  exists yet; only their invalidation types and atomic session boundary are ready.
- Freshness metadata is account-scoped. Consolidated multi-account results must check
  every participating account.
- Stale payload deletion is unnecessary because this stage does not persist derived
  financial payloads.

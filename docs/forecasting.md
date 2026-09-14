# Coverage-aware forecasting data and baselines

## Finalized-workspace balance forecast

The normal redesigned UI has a deliberately conservative adapter in
`cashflow_ai.workspaces.forecasting`. It accepts only a finalized canonical workspace,
its current revision, and a horizon of 15, 30, 60, or 90 days. It returns either an
exact tomorrow-through-horizon daily balance path or a typed `withheld` result; a
refusal is a normal safety outcome and the UI draws no substitute graph.

Availability requires confirmed, source-detached rows; no unknown financial roles; a
latest verified balance; at least 60 recent covered days; no gap intersecting that
training window; and coverage/balance evidence no more than 45 days old. Future-dated
evidence also blocks a path. Transfers affect this single account's balance but remain
excluded from the spending donut and external income/expense totals.

The adapter uses the mean observed signed cash movement for each weekday across the
latest 60 covered days. A fixed-seed residual bootstrap supplies an 80% empirical
interval, with a minimum daily uncertainty so flat history never creates a false
zero-width guarantee. If the last verified balance predates today but remains fresh,
the same baseline bridges only uncovered days and discloses that limitation.

The session workspace does not yet carry user-confirmed recurring-series membership.
For that reason this adapter labels its input as **unclassified total account cash
flow**: it does not call any portion discretionary and does not claim to project
confirmed recurring events separately. The UI discloses this limitation on every
available path. The older model pipeline retains its explicit recurring/discretionary
composition for developer regression use.

This is not presented as the advanced primary model. Every row in a one-shot upload
became known together at workspace finalization, so a claimed historical personal
backtest would backdate knowledge. The returned metadata explicitly says no historical
backtest or advanced-model selection was performed. Use:

```bash
make demo-workspace-results
```

The deterministic synthetic output includes available 15- and 30-day ranges and a
separate unknown-role case that is withheld. Intervals are estimates, not guarantees,
and uncertainty grows over longer horizons.

### Synthetic 60/30 chronological backtest

To make the forecast comparison inspectable without using a real statement, run:

```bash
make demo-workspace-backtest
```

The command creates one reproducible fictional 90-day ledger in the supported
Revolut consolidated-v2 structure and splits it at a fixed chronological cutoff.
Days 1-60 are first written to
`data/demo/generated/forecast-backtest/fictional_revolut_consolidated_v2_training_60_days.csv`.
That exact file passes through the real workspace CSV review, explicit synthetic-row
edits and temporary-workspace finalization. Its fictional dual-currency section
exercises the visible non-GBP exclusion warning and explicit scope confirmation.
Only the resulting finalized 60 GBP rows are supplied to forecasting.

After the forecast exists, days 61-90 are written to
`fictional_revolut_consolidated_v2_evaluation_30_days.csv`, the complete source is
written to `fictional_revolut_consolidated_v2_master_90_days.csv`, and the hidden
actual balances are revealed for scoring. All three generated files are ignored by
Git and use only fictional descriptions. The order is deliberate: evaluation and
master files do not exist when the forecast is produced.

The output reports final predicted versus actual balance, signed final error, daily
mean absolute error (MAE), daily root mean squared error (RMSE), and empirical
interval coverage. This is a synthetic chronological holdout check of the transparent
weekday-mean baseline through the same finalized-workspace boundary used by the UI.
It proves the code keeps later outcomes out of fitting; it does not prove accuracy on
a real user, every future month, or an advanced ML model.

Commit 22 creates trustworthy inputs and simple references, not an ML forecast. The
daily calendar uses the intersection of verified coverage across selected accounts.
Covered dates with no eligible transactions are zero; uncovered dates and dates with
an unresolved financial role remain null.

Weekly targets require seven covered days. Confirmed recurring expenses are
separated only after their confirmation evidence is available. Features require
eight immediately preceding weeks, so a gap breaks lags until sufficient consecutive
history returns. Features include lags, rolling means, payday distances, month, ISO
week, and known recurring outflow.

## Point-in-time availability

The dataset plan supplies an explicit timezone-aware knowledge cutoff. A covered
daily observation records `known_at`, the latest time needed to establish its
statement coverage, full UTC calendar day, trusted import lineage, transaction
verification, role decision, and recurrence membership. A partial current day stays
unknown even if its statement range already claims coverage. A weekly target takes
the latest availability of its seven complete days, and a
feature row retains both its Monday `forecast_origin_at` and the target's
`target_known_at`. A historical value can be used as a lag or training outcome only
when it was available strictly before that forecast origin. This prevents a later
role correction, statement import, recurrence confirmation, or member refresh from
appearing in an earlier prediction.

Eligible transactions must retain one-account lineage from the selected owned
account through the verified row, confirmed raw row, and import batch. Raw and batch
source types must agree. Confirmed CSV rows may come from verified or
`needs_review` batches because that batch state can represent retained row-level
issues; digital-PDF and OCR-PDF rows require a verified batch. Future imports,
unconfirmed raw rows, source mismatches, and cross-account links are excluded.

A confirmed recurrence schedule is anchored only to trusted members whose complete
evidence existed by the user's confirmation time. A later identified, older-dated
member can remove that observed transaction from discretionary spending from its
own `identified_at` time onward, but cannot move the already confirmed schedule in a
historical fold. The dataset also records a cutoff-bound recurring-outflow amount
for the week immediately after its latest complete target.

This makes a practical limitation visible: uploading a year-old statement today does
not prove that CashFlow AI knew those values throughout that year. Its historical
weeks have today's availability, so the service will not backdate them to manufacture
a June-to-August backtest. The advanced model therefore remains ineligible when no
honest historical folds exist. The same upload can still support a cautious future
path once its latest eight complete weeks are known before a future Monday: if the
latest covered week is not adjacent to that Monday, the path uses the recent
four-week-mean fallback, leaves uncovered dates unknown, widens uncertainty, and
returns a `recent_history_gap` warning. A genuine historical backtest still requires
contemporaneous imports/audits or another trustworthy timestamped snapshot.

The interactive UI keeps two dates separate. **Statement history ends** bounds the
transaction dates being summarised. The **knowledge cutoff** is the UTC click time
and determines which imports, reviews, balances, and recurrence confirmations were
actually available. Using the previous midnight for both would incorrectly hide an
import completed later the same day.

## Manual verification

Run:

```bash
make demo-forecast
```

Expected output includes `weekly targets: 20`, `leakage-safe feature rows: 12`,
`final test weeks: 3`, and MAE for five baselines. Each baseline is evaluated as a
rolling one-week-ahead prediction: after a test week's outcome becomes available it
may enter the next week's history, exactly as it may for the model.

To demonstrate a missing statement week:

```bash
uv run cashflow-forecast-demo --weeks 24 --test-weeks 3 --gap-week 10
```

Expected output includes `gap retained`. You may safely vary `--weeks` (minimum 13),
`--test-weeks` while leaving eight lag weeks plus training and validation, and
`--gap-week` from zero through `weeks - 1`; invalid combinations fail with a readable
argument error. All inputs are synthetic.

For the integrated local UI, follow the isolated dashboard commands in
`docs/frontend.md`, open **Forecast & plans**, select **Balance forecast**, and click
**Generate forecast**. The generated one-year student statement is expected to use
`recent rolling mean` with `recent history gap`, `low confidence model`, and
`limited residual history` warnings. It must return a daily path rather than an API
error, and it must not claim held-out model or interval performance. Future salary
or bill occurrences remain absent until the user refreshes recurring patterns and
explicitly confirms them.

## Primary model and manual verification

Commit 23 uses histogram gradient boosting for nonlinear weekly regression. Expanding
validation repeatedly fits a fresh estimator on outcomes that were available before
one later forecast origin. It supports model development without shuffling time. The
separate final chronological block is withheld from that development comparison and
acts as the last check on later consecutive weeks.

The candidate and every baseline receive the same information at each origin.
Selection requires the configured relative MAE improvement in both expanding
validation and the final test, while RMSE may not regress beyond its configured
allowance and absolute bias may not increase beyond its configured amount. Failure
of any gate selects an executable simple baseline; insufficient history selects the
recent four-week mean without inventing zero-valued evaluation metrics.

Permutation importance is calculated only on the held-out final block. Its signed
`mae_increase` records how much shuffling a feature changes error: positive values
suggest useful held-out signal, while zero or negative values must remain visible and
must not be presented as proof of causality. Multi-week paths start in Commit 24.

Run:

```bash
make demo-forecast-model
```

Expected output includes the selected model, whether the advanced candidate passed,
the candidate and best-baseline final MAE, and a controlled top-feature name. It also
prints `next forecast week:` and `predicted discretionary spending:`. The prediction
amount is deliberately not fixed in this document because it follows the explicit
synthetic parameters and fitted estimator.

Test safe fallback behavior with:

```bash
uv run cashflow-forecast-model-demo --weeks 36 --test-weeks 4 --flat
```

Expected output includes `advanced selected: false` and an executable baseline in
`selected model:`. You may vary `--weeks` (minimum 22), `--test-weeks`, and
`--minimum-improvement` from 0 through 1. All data is fictional.

Inference never accepts a target value. The canonical builder derives a
`ForecastInferenceRow` from the latest eight consecutive, already known weeks and
the dataset's cutoff-bound expected recurring outflow. The recurring amount carries
its own evidence time; zero means no confirmed occurrence was known by that cutoff,
not a caller-supplied guess. The predictor accepts exactly the Monday after the latest
observed week and rejects any fitted outcome or recurring input learned at or after
that origin. It returns one non-negative discretionary-spending amount. Arbitrary
historical dates, skipped weeks, and multi-week recursive paths are rejected rather
than silently changing the question being forecast. The result also identifies its
forecast origin, selected model or baseline, whether the advanced model passed, and
the training knowledge cutoff so callers can explain how it was produced.

## Forecast intervals and daily balance paths

Commit 24 uses expanding-validation residuals as a local empirical error distribution.
It samples those errors with a fixed seed around each weekly point forecast, allocates
weekly discretionary outflow across weekdays using covered historical observations,
and combines it daily with confirmed signed recurring flows and one verified opening
balance. The result includes expected, lower, and upper balances for every requested
date. Interval calibration is evaluated only on Commit 23's final chronological test
and reports both empirical coverage and mean width.

Only evidence known by the shared forecast cutoff is eligible. A recurring series
must already be confirmed and active, and the balance must already be verified and
recorded. Stale transaction, coverage, or balance evidence remains visible in stable
warnings and widens the interval by an explicit policy multiplier. A baseline model
and limited residual history are likewise disclosed. Scenario spending multipliers
and signed one-off events are hypothetical return values only; no database row is
written.

Run:

```bash
make demo-forecast-path
```

Expected output includes `selected model: hist_gradient_boosting`, `forecast days:
30`, `confirmed recurring events: 2`, `expected balance in 30 days:`, `likely range:`,
`warnings: none`, and held-out interval coverage. All values are fictional.

To inspect stale-data widening and a one-off hypothetical £50 outflow:

```bash
uv run cashflow-forecast-path-demo --horizon-days 14 --simulations 100 --stale-balance-days 10 --scenario-outflow 50
```

Expected output includes `warnings: stale_data`. You may safely vary
`--horizon-days` from 7 through 90, `--history-weeks` from 8 upward,
`--simulations` from 100 through 20,000, and non-negative scenario or stale-day
values. These ranges belong to the demo; the typed service permits horizons up to
365 days.

# Transaction anomaly review

Commit 25 adds a local, read-only detector and Commit 36 exposes its results through
a review interface. It identifies transactions that may deserve attention; it does
not diagnose fraud, block a payment, change a category, or edit imported evidence.
The only user-facing labels are `Unusual`,
`Possible duplicate`, and `Needs review`.

## Evidence boundary

An `AnomalyDetectionPlan` names one owned profile, one or more owned accounts, an
as-of date, a timezone-aware knowledge cutoff, and every model/rule threshold. The
service reads only verified canonical rows whose confirmed raw row, import batch,
role audit, category decision, and source-verification state were available by that
cutoff. Digital and OCR PDFs require a verified document; a CSV may have a
`needs_review` batch only because confirmed rows and unrelated retained row issues
can coexist in that batch.

The lookback is divided into an earlier reference period and a later detection
window. Complete, overlapping, and the known portions of gapped statements prove
coverage. Partial, unknown, future-imported, and explicitly missing dates do not.
Coverage sufficiency is evaluated for every selected account, and the minimum result
is disclosed.

Pending records, transfers, unresolved/excluded roles, uncovered dates, and duplicate
evidence never enter Isolation Forest. Exact and probable duplicate rules remain
outside that model gate so a duplicate can still be shown as `Possible duplicate`.
If history or coverage is inadequate, the service returns careful rule-only results
with stable warnings instead of fitting an unreliable model.

## Explainable rules

The deterministic layer checks:

- exact and probable duplicate evidence;
- absolute amount against a robust historical median/MAD threshold;
- high spending at a merchant absent from the reference period;
- an explicitly confirmed recurring charge above its expected amount;
- a charge dated after an explicit cancellation;
- total spending on a covered day against a robust daily threshold; and
- a verified transaction carrying a negative running balance.

MAD means median absolute deviation: the median distance from the historical median.
It is less distorted by one very large purchase than an average and standard
deviation. Explicit minimum GBP thresholds prevent a nearly flat history from making
tiny changes look extreme. A high-spend-day signal is attached only to that day's
largest eligible transaction so the review queue does not repeat one event on every
row.

A confirmed recurring payment is protected from generic large-amount, new-merchant,
daily-spend, and model-only alerts. It can still produce a specific recurring price
increase, duplicate, or negative-balance signal. A cancelled series produces a
charge-after-cancellation signal only on a later calendar date; transaction dates do
not contain enough time-of-day evidence to order a cancellation and charge on the
same day.

## Isolation Forest

Isolation Forest is an unsupervised candidate: it does not need transactions labelled
“fraud” or “normal.” It builds many random decision trees and tends to isolate unusual
feature combinations in fewer splits than common combinations. The model is fitted
only on the covered reference period and scores only the later detection window.

Its eight conceptual features are log absolute amount, prior merchant frequency,
category, weekday, days since the previous merchant transaction, difference from the
prior merchant median, difference from the prior category median, and merchant
novelty. Category is encoded into model-only numeric indicator columns. Feature state
is updated chronologically: a row's values are constructed before that row becomes
history, so a transaction cannot normalise itself.

The returned model score is a bounded ranking aid derived from Isolation Forest's
decision margin. It is not a probability of fraud. The configured contamination is a
model threshold assumption, not the expected real fraud rate. A fixed estimator
count and random seed make the same synthetic scan reproducible. The local model
registry can persist aggregate run metadata without alerts or transaction-level
scores. The fitted model remains in memory and the unsupervised candidate is not
activation-eligible without a labelled evaluation gate.

## Explicit review feedback

Detection remains read-only. When the user selects `expected_activity` or
`confirmed_unusual`, the API re-runs the exact profile/account/date/policy scan and
refuses the write if that transaction is no longer an alert. It then upserts one
existing `anomaly_alerts` row with only the transaction identifier, bounded score,
controlled comma-separated signal codes, and `dismissed` or `reviewed` status.
Descriptions and merchant text are not copied into the feedback record.

Later scans attach that saved status to the still-current suggestion. A review does
not edit the verified transaction, category, role, imported source row, forecast, or
model. It also does not provide a labelled evaluation set or trigger retraining.

## Manual verification

Run the complete fictional example:

```bash
make demo-anomalies
```

Expected output includes `detection mode: rules_and_model`, `Possible duplicate`,
`Needs review`, `known recurring rent protected: yes`, `warnings: none`, and the
statement that review signals are not confirmed fraud.

Run the same service with deliberately inadequate statement coverage:

```bash
uv run cashflow-anomaly-demo --history-transactions 30 --sparse
```

Expected output includes `detection mode: rules_only` and the warnings
`insufficient_coverage, insufficient_history`; deterministic duplicate and balance
rules still remain inspectable. You may safely vary `--history-transactions` from 20
through 200. All names, dates, amounts, imports, and balances are fictional and the
database exists only in memory.

For the manual interface path, start `make api` and `make ui`, open **Forecast &
planning → Anomaly review**, select only fictional accounts, and enable the scan.
Review a fictional alert as expected or unusual and run the scan again. The item
should display its saved status; its source transaction must remain unchanged.

## Current limitations

Unsupervised outliers can be legitimate and ordinary-looking transactions can still
be harmful. Synthetic tests prove logic, leakage boundaries, and failure behaviour;
they do not establish a real-world detection rate. The product persists explicit
review feedback, not every generated alert, and does not learn from dismissals or
confirmed-unusual choices. It also cannot reconstruct a recurrence series state that
was later overwritten in the current lightweight schema.

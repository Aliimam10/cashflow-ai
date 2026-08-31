# Evaluation

This document records reproducible evaluation policy and, in later release
stages, results for categorisation, recurring-payment detection, forecasting,
anomaly detection, and product performance. Automated tests establish pipeline
correctness with fictional data; they are not a benchmark on real bank data.

Every advanced model must be compared with a meaningful baseline. Forecast
evaluation will use chronological cutoffs and must include leakage checks,
failure analysis, uncertainty coverage, and sparse-data behaviour.

## Transaction category classifier

Commit 19 evaluates one candidate architecture: word and character TF-IDF
followed by Logistic Regression. It compares the candidate with a
most-frequent-category baseline on the same two holdouts.

### Chronological holdout

The caller supplies an explicit historical boundary. Training transaction dates
must precede the test dates; rows are never shuffled and one calendar date cannot
cross the boundary. Only verification and category decisions available by the
historical cutoff may enter training. Financial roles are reconstructed from
role-change audits available at that cutoff; without an audit, the role is
`unknown`. Feature normalisation, both TF-IDF
vectorisers, and Logistic Regression are fitted only on that training partition.

This holdout measures performance on later activity while preventing future
category corrections and future vocabulary statistics from leaking backwards.

### Unseen-merchant holdout

The second split groups rows by normalised merchant. Its deterministic test
merchant groups are entirely absent from training. The candidate and baseline
are fitted again from scratch; neither reuses the chronological model or
vectorisers. Rows without a usable merchant do not count as proof of
unseen-merchant generalisation.

This holdout tests whether description and subword patterns generalise beyond
memorised known merchants.

### Reported results

For both candidate and baseline, each holdout records:

- macro F1, which gives each represented category equal weight;
- weighted F1, which reflects category frequency;
- precision and recall;
- per-category precision, recall, F1, and support; and
- a labelled confusion matrix with deterministic category order.

Undefined class metrics use a controlled zero rather than producing unstable
warnings. The report also records split dates, row counts, separate historical-
and final-dataset exclusion counts, class support, merchant-group diagnostics,
parameters, random seed,
taxonomy version, feature-schema version, and knowledge cutoffs.

The candidate-selection recommendation requires macro F1 to beat the baseline
on both holdouts and weighted F1 not to regress on either. Passing this comparison
does not activate the model, define a confidence threshold, or prove performance
on a particular user's future transactions. Failing it leaves an inspectable
evaluation result and records the candidate as not selected for the later hybrid
workflow.

No real-data scores are published at this stage. Release documentation must
clearly distinguish synthetic test outcomes from a reproducible evaluation on an
appropriately reviewed dataset.

## Commit 20 selection gate

Hybrid inference consumes Commit 19's derived `candidate_selected` result. A model
that failed either required holdout comparison is rejected. Per-row confidence only
controls apply-versus-review behavior and does not replace evaluation.

## Forecast baseline evaluation

Expanding validation and final testing answer different questions. Expanding folds
repeatedly grow the training history and predict one later, fully covered week; they
are the development evidence used to check whether performance is stable through
time. The final test is the last requested consecutive eligible block and remains
outside those development fits. Unknown actual dates cannot produce targets, and an
outcome whose `target_known_at` is not before a forecast origin cannot enter that
origin's training history.

All five baselines are executable and roll one week at a time. Historical mean and
seasonal naive use only targets revealed by that origin; recent mean uses the same
past-only feature row; recurring-only and zero-discretionary provide the explicit
zero floor. After a test outcome becomes available, it may enter the next prediction
for both baseline and candidate. This information-equivalent protocol prevents the
model from competing against a baseline frozen at an older history. Metrics are
weekly GBP MAE, RMSE, and signed bias, reported separately for expanding validation
and the final test.

Commit 23 reports gradient-boosting MAE, RMSE, and bias for both comparisons. At each
expanding origin it fits a fresh estimator only on earlier available outcomes; the
final estimator fits the eligible pre-test history and predicts the final block.
Horizon-one performance is reported explicitly; multi-horizon paths belong to Commit
24.

Selection is deliberately conjunctive. On both expanding validation and the final
test, candidate MAE must beat that comparison's best simple baseline by more than the
configured relative margin, candidate RMSE must remain within the configured
relative regression allowance, and candidate absolute bias must remain within the
configured additive allowance. Any failure keeps the appropriate executable
baseline in control. Low-data fallback results leave evaluation metrics absent rather
than representing “not evaluated” as zero error.

Permutation importance is calculated against final held-out MAE. The reported
`mae_increase` stays signed: a negative value means shuffling happened to improve
held-out error and is evidence against claiming that feature helped. Importance is a
diagnostic for this sample, not causal or financial-advice evidence.

## Forecast interval evaluation

Commit 24 calibrates residual-bootstrap intervals from expanding-validation errors,
not the final chronological test. The final test then reports empirical interval
coverage—the proportion of actual weekly outcomes inside the configured range—and
mean interval width. Coverage without width can reward uselessly broad intervals, so
both remain visible. A low-data fallback with no independent final test reports no
interval-performance result rather than inventing zero error or perfect coverage.

Future weekly centres are recursive after the first week. Bootstrap residuals model
observed forecast error around those centres; they are not a guarantee, a causal
confidence interval, or proof that future behaviour matches history. Explicit
minimum uncertainty prevents a perfectly flat or tiny calibration sample from
claiming certainty. Baseline selection and stale evidence multiply interval width by
caller-visible policy values. The deterministic seed makes synthetic evaluation and
manual verification reproducible.

## Transaction anomaly evaluation boundary

Commit 25 does not have reviewed real-world anomaly labels, so supervised metrics
such as precision, recall, false-positive rate, or fraud loss would be fabricated.
The rule layer is the meaningful transparent baseline: every rule has a controlled
reason and synthetic positive/negative case. Isolation Forest adds only a review
signal and must not be described as outperforming those rules or detecting fraud.

Automated evaluation verifies that the reference period precedes the detection
window, current rows do not update their own past-only features, category levels are
fit on reference data, and a fixed seed reproduces scores. It also verifies
per-account coverage/history gating; pending, duplicate, transfer, unresolved, and
uncovered exclusions; protected confirmed recurrence; every specified rule; careful
wording; and safe rules-only fallback.

The contamination parameter sets an outlier decision threshold within Isolation
Forest. It is not a measured fraud prevalence. The transformed bounded score is a
ranking aid rather than a calibrated probability. A later reviewed dataset can add
precision/recall and alert-volume analysis without weakening the current leakage and
privacy gates.

## Registry evaluation records

The local model registry preserves only aggregate metrics already produced by each
model-specific evaluation. It does not recalculate evaluation, weaken a selection
policy, or infer missing scores. Categorisation records candidate and baseline
classification metrics for both required holdouts. Forecasting records candidate and
baseline MAE, RMSE, and bias for expanding validation and final testing when those
results exist. Low-data fallback records its sample count without inventing error
metrics. Anomaly detection records run sufficiency diagnostics but remains
activation-ineligible because it has no labelled accuracy benchmark.

Activation eligibility comes from the owning evaluation boundary. Registry
activation is a later explicit selection action; merely inserting a row never makes
the model active.

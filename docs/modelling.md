# Modelling

CashFlow AI uses simple, explainable baselines before considering more advanced
models. Commit 19 implements the first supervised candidate: a local transaction
category classifier. It does not activate that classifier, assign categories, or
change the deterministic rule service.

Forecasting will later model confirmed recurring flows separately from
discretionary cash flow. All model training and backtesting must respect
historical cutoffs and exclude future information.

## Transaction category dataset

The category dataset is built from one owned profile's persisted records. A row
is eligible only when its transaction and source lineage are trusted and an
explicit category correction is available at the applicable knowledge cutoff.
The builder uses category-correction history rather than the current category
column so a value created later cannot silently become an earlier label.
Financial roles are reconstructed independently from role-change audits at the
same cutoff; a transaction without an audit is `unknown`, even if its current
stored role has since changed.

For a chronological evaluation, the training side uses only transactions that
were verified and category decisions that were known before the historical
boundary. The evaluation side may use labels known by the later overall cutoff
as ground truth. The final candidate may train on all eligible labels available
by that overall cutoff after evaluation is complete.

The builder excludes:

- missing, inactive, or `needs_review` category targets;
- unknown or deliberately excluded financial roles;
- unresolved transfer and structured review items;
- exact or unresolved probable duplicates;
- unconfirmed source rows; and
- digital-PDF or OCR rows whose statement has not been fully verified.

Exclusions are returned as aggregate controlled counts. They do not delete any
source record, and error messages do not repeat private descriptions or merchant
names.

## Features and estimator

Only the verified merchant and description are used as text features. They are
normalised into a temporary, versioned representation with stable field markers;
the stored values remain unchanged. Amount, balance, account identity, raw source
payload, statement context, free-text notes, and extraction text do not enter the
model.

The scikit-learn pipeline combines:

- word TF-IDF unigrams and bigrams;
- character-within-word TF-IDF three-to-five-character n-grams; and
- Logistic Regression with deterministic configuration and balanced class
  weighting.

Word features capture useful phrases, while character features tolerate modest
merchant abbreviations and reference suffixes. The vectorisers and classifier
are always fitted together inside a pipeline. Each holdout gets a new pipeline,
so test vocabulary and inverse-document-frequency statistics cannot leak into
training.

## Evaluation and candidate selection

The chronological holdout never shuffles time. All training transaction dates
precede the explicit test-start date, and the same date cannot appear on both
sides. The unseen-merchant holdout groups normalised merchant names and guarantees
that every test group is absent from its training rows. Rows without a usable
merchant can still help the final and chronological model, but cannot demonstrate
unseen-merchant performance.

A most-frequent-category classifier is fitted separately on each identical
training partition. The candidate and baseline both report macro F1, weighted
F1, precision, recall, per-class support, and stable labelled confusion matrices.
The recorded selection rule requires improvement in macro F1 on both holdouts
without worse weighted F1. This is an evaluation recommendation, not activation
or a claim of production readiness.

The trainer reports a controlled insufficiency instead of reshuffling dates or
moving merchants when there are no eligible labels, fewer than two categories,
too few rows under the explicit policy, no chronological boundary, no usable
merchant split, a one-class training partition, or empty searchable text.

## Candidate artefacts

An evaluated candidate may be stored as a joblib model plus a JSON metadata
sidecar in an ignored local model directory. Existing versions are not silently
overwritten. The loader verifies its checksum, format, feature schema, taxonomy,
and scikit-learn version, then checks the fitted word/character pipeline,
Logistic Regression parameters, and class order before inference.

Joblib deserialisation can execute code. Only artefacts produced locally by this
application and kept inside the trusted local boundary may be loaded. Downloaded,
emailed, or otherwise untrusted model files must never be opened.

The model contains its learned TF-IDF vocabulary and is therefore private even
though it does not deliberately retain complete training rows. The sidecar omits
descriptions, merchant names, transaction IDs, profile IDs, and account IDs. It
contains controlled versions and parameters, aggregate class counts, separate
historical and final dataset exclusion counts, training dates and cutoffs, split
policy, baseline and candidate metrics, selection result, software versions,
creation time, and the model checksum.

Commit 20 will decide when rule fallbacks may call an eligible candidate, apply
confidence thresholds, queue uncertain predictions, and record user feedback.
Commit 26 will provide the lightweight database registry and active-model
lifecycle. Commit 19 performs neither responsibility.

## Synthetic data

`cashflow_ai.demo_data` produces deterministic one-to-three-year histories for
student, salaried-worker, and irregular-income profiles. Each history includes
recurring flows, discretionary purchases, price drift, running balances,
labelled unusual transactions, and exact and probable duplicate examples.

The generator uses only generic fictional merchants and configurable random
seeds. It is test data and demonstration data, not evidence of model quality.
Automated model tests use synthetic examples to verify behaviour, splits, and
metric calculation; they do not establish real-world classification accuracy.

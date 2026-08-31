# Privacy

CashFlow AI is designed for local-first use and does not require bank
credentials.

Repository rules:

- do not commit real statements or personal transaction data;
- use synthetic data in examples and tests;
- keep uploads, local databases, processed private data, secrets, and model
  artefacts outside version control;
- do not include private descriptions in logs or screenshots;
- preserve raw imported rows for local audit while applying safe retention and
  deletion controls in later stages.

## Import fingerprints

- Treat source and canonical transaction fingerprints as private local metadata.
- A SHA-256 fingerprint helps compare exact or cleaned values but is not
  anonymisation and must not be published as a privacy substitute.
- Do not place original transaction descriptions or fingerprint input material
  in normal logs.
- Keep original source values beside cleaned values locally so a user can audit
  or correct the import; never overwrite the original evidence during cleaning.
- Bind confirmation to the exact preview hash. Re-parse the confirmed bytes and
  reject the operation if that identity changed.
- Treat client-reported confirmation time as untrusted request metadata. Reject
  a future value and use one server UTC receipt time for every persisted CSV
  evidence timestamp, preventing a backdated request from leaking newly uploaded
  private data into an earlier historical cutoff.
- Preserve rejected and probable-duplicate rows locally for audit, but exclude
  them from verified calculations until an explicit later review decision.
- Treat manual, statement, and running-balance snapshots as private financial
  data. Do not expose their amounts, dates, source document links, or freshness
  results in normal logs.

## Local database

- The Version 1 database is a local SQLite file and must remain ignored by Git.
- Treat the database, SQLite journal/WAL files, backups, and inspection exports
  as private financial data.
- SQLite foreign keys are enabled so deletion and lineage rules are enforced
  rather than silently leaving orphaned private records.
- Database migrations define structure only; they must never contain real user
  data or statement-derived fixtures.

## PDF and OCR handling

- Process digital and scanned statements locally by default.
- Treat uploaded PDFs, extracted text, page images, OCR crops, and intermediate
  tables as private financial data.
- Use generated safe filenames and never place an original filename or statement
  text in normal logs.
- Remove temporary page images and OCR artefacts after confirmation or
  cancellation unless the user explicitly chooses local retention.
- Display extraction confidence and require review before persistence.
- Do not send statement pages or OCR text to an external service without a
  separate, explicit privacy decision and user consent.

The embedded-text PDF adapter processes uploaded bytes in memory. It returns
page text and candidates only to the caller for a later local review and does
not create temporary files, database records, or normal-log entries. Committed
tests generate fictional PDFs in memory; the repository contains no real or
redacted personal statement fixture.

The OCR adapter also processes pages locally. PyMuPDF renders each page into an
in-memory image; Pillow performs rotation and preprocessing; and pytesseract
invokes the locally installed Tesseract executable. Page images are closed after
each recognition attempt, no application-managed image file is retained, and
raw OCR text is returned only in the review preview. The adapter does not write
OCR results to the database or normal logs. Automated tests create fictional
scanned PDFs in memory and use a deterministic fake OCR engine.

The reconciliation and review service is also in-memory. It records confidence,
issues, decisions, and corrections without logging source descriptions. User
corrections create a separate working/canonical value and never mutate the
original extracted evidence. Transaction corrections cannot change the extracted
account, currency, category, or financial role. Choosing a date interpretation
reparses ambiguous transaction and posting dates while leaving their raw text
untouched.

Raw balance evidence keeps its exact document and source-adapter identity, page,
line, provenance, and local OCR confidence. Confirmed or corrected balance values
remain paired with that evidence even when arithmetic reconciliation is
unavailable. Approved rows retain their full raw lineage beside canonical values,
and rejected rows retain their complete unchanged review evidence rather than
being silently discarded. Only a statement-level approval bound to the exact
document hash can produce trusted in-memory rows; this stage has no PDF database
write or review UI, and unverified OCR candidates remain ineligible for every
downstream calculation or model input.

The persistence boundary must not store an approved PDF balance in isolation.
Until the complete PDF unit of work exists, balances remain inside the approved
in-memory result so the source rows, rejected evidence, confirmed coverage, and
document lineage cannot be separated from them.

Freshness assessment reads only verified, non-future evidence and returns dates,
ages, warnings, and a readiness mode without logging source descriptions or
balance amounts. A manually entered balance is deliberately not disguised as a
transaction or import. Missing or unverified coverage remains unknown and cannot
be converted into an apparent zero-spend period.

Financial-role rules operate locally and do not log descriptions, amounts,
notes, suggestions, or decisions. Controlled reason codes contain only rule
facts, never copied statement text. Free-text statement notes remain visible as
local review context but are not parsed into a role. Confirming or overriding a
role updates only the verified interpretation and append-only audit history;
the original raw payload and description remain unchanged.

Financial-role confirmation, rejection, override, and review-flag history uses the
server's aware UTC receipt time. Caller-reported review times are not stored, so a
backdated value cannot leak a decision into an earlier training, recurrence,
analytics, or forecasting cutoff. Receipt time is still private behavioural metadata
and must not be emitted to telemetry with transaction context.

Coverage-aware analytics also runs locally and read-only. Repository queries do
not load raw payloads, statement notes, flags, pending suggestions, or rejected
rows. Returned descriptions, amounts, balances, categories, coverage ranges, and
derived totals are private financial data intended for the local caller; they
must not be placed in normal logs, telemetry, committed snapshots, or test
fixtures based on a real person. Automated analytics tests use fictional values
only. No derived analytics report is persisted by this stage.

Deterministic categorisation also runs locally. Its repository does not load raw
payloads, import notes, or flags, and no categorisation path may use those values
as hidden rules. Matching uses temporary normalised copies of the verified
merchant and description; it never rewrites source text. A category assignment
changes only the verified interpretation and cannot change financial role,
amount, date, account, currency, source lineage, or extraction evidence.

Category explanations expose controlled reason codes, rule identities, and field
names only. They do not echo matched merchant text, description phrases, amounts,
account identifiers, or free-text statement notes into logs or telemetry.
Repository merchant and keyword configuration contains generic public rules,
never a real person's transaction history. Personal rules remain private local
inputs and are not persisted by Commit 18. Commit 20 stores one only after an
explicit scoped feedback choice; deletion controls remain later interface work.
Committed categorisation tests use fictional values only.

ML categoriser training remains local and uses a narrow verified-data query.
The model feature boundary receives only temporary copies of the verified
merchant and description. It does not query or transform raw payloads, statement
notes, account names, amounts, balances, extraction text, or OCR confidence into
features. Eligibility failures are reported as aggregate controlled counts;
normal logs and metadata must not contain example text, merchant names, or
transaction, profile, and account identifiers.

Historical label reconstruction reads category-correction timestamps so a future
decision cannot leak into an earlier training fold. Unverified PDF/OCR documents,
unconfirmed raw rows, unresolved duplicates or transfers, unknown and excluded
financial roles, and `needs_review` categories remain outside the supervised
dataset. Financial roles come only from role-change audits available at the
cutoff; the current role column is never historical truth, and an absent audit
means `unknown`. Filtering never deletes or overwrites retained local evidence.

A fitted TF-IDF vocabulary can contain fragments of private descriptions and
merchant names. The model artefact is therefore private financial data even when
complete training rows are not serialised. Model files and their metadata
sidecars belong in ignored local directories and must not appear in Git, support
bundles, telemetry, screenshots, or test snapshots. Committed model tests use
fictional synthetic text and temporary directories only.

Sidecar metadata is data-minimised to controlled versions and parameters,
aggregate class counts, cutoffs and date ranges, evaluation metrics, selection
status, separate historical and final dataset exclusion counts, software
versions, creation time, and an artefact checksum. It does not
contain learned vocabulary or row-level data. These aggregates can still reveal
information about local activity, so the sidecar remains private and ignored.

The SQLite model registry applies the same minimisation to database metadata.
Categorisation confusion matrices and per-category support, forecast held-out actuals
and predictions, anomaly alerts, descriptions, merchant names, learned vocabulary,
transaction/account/profile identifiers, and statement notes are not copied into a
registry row. It retains controlled model identities, aggregate metrics, training
dates, features, parameters, selection state, and a relative `models/` artefact path.
Absolute paths are rejected because they may reveal a username or filesystem layout.
The local database and referenced artefacts remain ignored private runtime data.

Planning calculations query only verified transaction amounts, controlled categories
and financial roles, statement coverage, persisted budget/goal values, and reduced
forecast summaries. They do not read raw payloads, merchant names, descriptions,
statement notes, OCR text, or model vocabulary. Returned warnings use controlled
codes and record identifiers rather than echoing free-text goal names. The synthetic
manual demo uses an in-memory database and fictional values.

Joblib model loading is a trusted-local operation because deserialising an
untrusted file can execute code. A checksum detects accidental replacement but
does not make a downloaded model trustworthy. CashFlow AI must load only
artefacts it created within its local trust boundary and whose expected model,
feature-schema, and taxonomy versions match. Commit 19 neither uploads a model
nor registers it in the database.

## Hybrid decision privacy

Decision audits and review items contain controlled identifiers, category,
probability, source, versions, and reason codes—not raw payloads, descriptions,
statement notes, or account names. Text is used transiently by the local model.
Personal rules remain local private data. Authoritative server receipt time prevents
current feedback from being backdated into older training truth, and manual
retraining retains Commit 19's historical-cutoff and ignored-artifact safeguards.

## Recurrence privacy

Detection runs locally and stores normalised merchant grouping plus derived schedule
evidence. It does not copy raw payloads, notes, account names, or original
descriptions into recurrence records or logs. Candidate evidence/cutoff timestamps
and member `identified_at` values are still private behavioural metadata and must not
appear in normal logs or committed snapshots. As-of reconstruction reads source,
role-audit, coverage, and membership evidence available by the cutoff; it does not
rewrite a newer stored candidate merely to answer an older historical request. Tests
and the recurrence demo use fictional data only. Recurrence review state and a
confirmed series use one authoritative server UTC receipt time. A caller-reported
review time is aware-validated but never stored, so backdating cannot expose a current
confirmation or cancellation to an earlier historical calculation.

## Forecast-data privacy

Construction reads canonical amounts, dates, roles, coverage, and confirmed
recurrence membership locally. It does not read raw payloads, descriptions, merchant
text, notes, or flags and does not persist feature rows. The manual demo uses fixed
fictional weekly amounts.

`known_at`, `forecast_origin_at`, and `target_known_at` are privacy-relevant metadata
because they reveal when local financial evidence became available. They must remain
inside the same local boundary as amounts and coverage. The service preserves these
times to prevent future statement imports, role changes, or recurrence decisions from
leaking into a historical fold. It does not backdate a statement uploaded today to
manufacture past model knowledge.

The Commit 23 estimator receives numeric feature copies only. It never receives
merchant names, descriptions, raw rows, notes, account names, or identifiers. The
inference row is target-free and represents exactly one next week. Comparison
metrics, signed feature importance, and predictions are also derived private
financial data even without transaction text; they must not enter telemetry or
committed fixtures based on a real person. The model remains in memory and is not
committed or persisted in this stage.

Commit 24 reads numeric forecast features, verified balance evidence, controlled
financial roles, recurrence dates/amounts, and cutoff timestamps locally. It does not
read or return merchant text, transaction descriptions, raw payloads, notes, account
names, or statement files. Residuals, simulated paths, scenario assumptions, interval
metrics, and daily balances remain sensitive derived financial data even without
descriptions and must not be logged or added to real-data fixtures.

The service returns an in-memory result and writes no scenario, forecast-run, model,
transaction, or balance row. Manual verification uses fixed fictional identities and
amounts. The CLI's £ symbol and outputs are synthetic demonstration text, not a real
account export or financial advice.

## Anomaly-review privacy

Anomaly detection runs entirely inside the local process. Merchant and category
history is used transiently to construct features, but returned signals contain only
controlled reason codes, transaction/account identifiers, bounded scores, and
optional numeric comparisons. Raw payloads, descriptions, merchant text, statement
notes, filenames, and account names are not copied into alerts or normal logs.

Even data-minimised anomaly scores, merchant frequencies, category levels, balances,
and alert identities are derived private financial data. Results and the in-memory
fitted model must not enter telemetry, committed fixtures, screenshots based on a
real user, or support bundles. All automated tests and the manual CLI create only
fictional records in an in-memory SQLite database.

Commit 25 performs no write to transactions, imports, anomaly alerts, or model
metadata. It neither stores the fitted Isolation Forest nor learns automatically
from user review. Later persistence must preserve this local boundary, avoid raw
feature text in metadata, and record a review as an explicit user action rather than
silently using it as a training label.

# Privacy

CashFlow AI is designed for local-first use and does not require bank
credentials.

## Version 1 privacy position

The release is a local single-user application, not a hosted financial service.
FastAPI and Streamlit bind only to loopback, Tesseract runs locally, and no bank,
analytics, telemetry, advertising, cloud-model, or external OCR API receives a
statement. Docker preserves the same boundary and stores SQLite/model files in local
named volumes.

Local-first does not mean risk-free. Anyone who can access the operating-system
account, SQLite file, Docker volumes, process memory, browser session, backups, or a
user-created screenshot may see financial information. Version 1 does not encrypt the
database, manage OS permissions, automate backups or deletion, authenticate users, or
protect a remotely exposed port. Users remain responsible for device encryption,
account access, backups, and secure removal.

### Release privacy checklist

- Use only synthetic fixtures, demonstrations, documentation images, and logs in Git.
- Keep `.env`, uploads, raw/processed data, SQLite files and journals, exports, logs,
  and model artefacts ignored and outside the Docker context.
- Verify that PDF/OCR bytes and recognised lines are not persisted or logged before
  the future atomic PDF import boundary exists.
- Verify API responses expose canonical user-facing fields rather than raw payloads.
- Inspect screenshots manually for names, institutions, account identifiers,
  balances, merchants, dates, filenames, browser history, and desktop notifications.
- Never publish the loopback-only application through port forwarding, a tunnel, or
  a remote host without a separate authentication, TLS, threat-model, and deployment
  stage.
- Treat model vocabularies, fingerprints, aggregate results from very small groups,
  and backups as potentially identifying private data.

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

Scenario comparison retains a user-supplied name/description only inside its local
in-memory request and response. It does not log that text or persist the scenario.
Compiled forecast adjustments contain controlled identities, dates, directions, and
amounts only. Evaluation never inserts or updates transaction, recurrence, budget,
goal, forecast-run, or scenario records. The manual scenario demo uses only fictional
values in an in-memory database.

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

## Derived-freshness privacy

Invalidation metadata contains account/output identities, integer revisions,
controlled source-change codes, statuses, and timestamps only. It never stores
transaction descriptions, merchants, raw rows, OCR text, balances, totals, model
features, predictions, scenario assumptions, or report payloads. Even the fictional
demo callback payload is returned in memory and absent from both metadata tables.

Change types intentionally say `category_changed` or `statement_added` without
copying the corrected category, filename, transaction identity, or amount. Normal
application logs may record the controlled status/code but must not add the private
source values that caused it.

## Local API privacy boundary

The first FastAPI server binds only to validated loopback hosts. This reduces network
exposure but is not authentication; it must not be made externally reachable until a
separate access-control and deployment design is reviewed.

Uploaded CSV/PDF bytes are bounded, processed in memory, and closed after each call.
The API creates no upload cache. Stateless confirmation means the exact source must
be supplied again and verified rather than storing an unreviewed document between
requests. CSV confirmation may then persist through the established audit-preserving
service. PDF approval remains non-persistent.

Transaction responses omit raw source payloads. Readiness checks connectivity and
schema names only. Central exception handlers return controlled codes/messages and
must not echo body values, raw rows, SQL, local file paths, subprocess output, or
tracebacks. FastAPI debug responses remain disabled even when general local debug
logging is enabled. OpenAPI describes schemas and routes but contains no runtime
financial records.

The API demo and automated route tests use fictional uploads and temporary databases.
The manual demo removes its database after completion. Real local statements and
databases remain governed by the repository ignore rules and must never be copied
into request examples, test failures, screenshots, or committed API documentation.

Decision-support endpoints rebuild analytics, recurrence, forecast, anomaly,
planning, and scenario results locally from verified owned records and explicit
cutoffs. They do not accept transaction rows, fitted models, prediction results, or
balance paths as trusted client evidence. Only revision/freshness metadata is stored
for calculated responses. Category and financial-role review endpoints return the
minimum context needed for a local user decision; model-information endpoints expose
aggregate registry metadata rather than artefacts, feature values, or training rows.

## Local frontend privacy boundary

The Streamlit launcher and API client accept only `127.0.0.1`, `localhost`, or `::1`
with explicit ports. The client uses plain HTTP only inside that same-machine boundary,
does not inherit proxy configuration, and never sends requests to an absolute path
provided by a page. Loopback binding reduces exposure but is not authentication; the
UI and API must not be published to a local network or the internet.

Streamlit session state stores only the selected page, optional local profile/account
identifiers, and whether the privacy notice was shown. It must not hold upload bytes,
raw or verified transaction text, amounts, balances, API payloads, model features, or
forecast results. Common errors render only controlled client messages and stable
codes; untrusted API bodies, URLs, source descriptions, and local paths are discarded.
The home page keeps both the privacy notice and forecast disclaimer visible.

The import page reads bytes only from the current upload widget and sends them to the
loopback API. It creates no application-managed upload file or preview cache. Raw
transaction text and extracted values are intentionally visible in the local review
screen but must not be copied into normal logs, screenshots, bug reports, or committed
fixtures. Profile/account setup requests descriptive local metadata only—not bank
credentials or account numbers. PDF approval is explicitly shown as non-persistent;
CSV persistence still occurs only after exact-file confirmation. Committed manual
fixtures are generated from fixed fictional statements under the ignored demo-data
directory.

The transaction workspace necessarily shows verified descriptions and fixed-precision
amounts on the same machine. It does not put search results, review items, dashboard
responses, or charts into the application-managed session record. Search and review
endpoints never return the complete raw payload. Probable-duplicate candidate
snapshots are private local database evidence, never logs or telemetry, and exist only
so an explicit keep decision does not guess financial fields from source-specific
columns.

The recurring and forecast interface keeps the same boundary. It sends only profile
and account identifiers, explicit cutoff dates, review actions, and documented policy
values to the local API. Candidate evidence and forecast results are rendered for the
current run but never copied into application-managed session state, files, telemetry,
or logs. Chart floats are presentation-only copies; fixed-precision source and API
money remains unchanged.

Budget/goal forms persist only the validated plan values the user explicitly saves.
Scenarios remain non-persistent and charts receive presentation-only floating-point
copies. Anomaly scans are read-only; an explicit feedback action causes the backend to
recompute the same scoped alert and retain only its transaction link, bounded score,
controlled signal-code reasons, and reviewed/dismissed status. No raw description or
merchant text is duplicated into the alert record, and feedback does not trigger
training. Model-information controls expose only aggregate registry metadata, never
learned vocabulary, feature rows, or transaction-level predictions.

## Privacy regression gates

The end-to-end safety suite uses only generated CSV bytes, an in-memory fictional PDF,
and a deterministic fake local OCR engine. It verifies safe upload basenames, raw-row
preservation, explicit OCR correction, closed page images, data-minimised transaction
responses, and the rule that an approved but unpersisted PDF cannot enter analytics.
Sensitive marker text is allowed in the local response needed for review but must not
appear in normal logs.

Focused tests continue to protect file and render limits, controlled error bodies,
unknown coverage, transfer double-counting, stale balances, scenario isolation, and
downstream invalidation. The test map and exact manual command are documented in
[`testing.md`](testing.md).

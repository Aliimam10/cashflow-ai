# Changelog

All notable changes to CashFlow AI are documented here. The project follows
[Semantic Versioning](https://semver.org/), and this history describes the
repository rather than implying that a hosted service has been deployed.

## [Unreleased]

### Added

- Added an isolated statement-workspace API and normal UI flow for combining several
  same-account GBP CSV and selectable-text PDF statements into one editable canonical
  table, including per-file mappings, source removal, exact/probable duplicate
  handling, optimistic row revisions, explicit coverage/balance confirmation, and an
  in-memory canonical CSV download.
- Added saved and temporary retention modes. Saved mode records only finalized
  canonical transactions, confirmed coverage, and optional balance evidence;
  temporary mode never enters SQLite.
- Added `make demo-workspace`, a fully synthetic mixed CSV/PDF walkthrough that
  demonstrates saved restoration, selected-workspace deletion, and API-shutdown
  cleanup of temporary state.
- Added confirmed controls for deleting either one selected workspace or every
  active and saved statement workspace.
- Added finalized-workspace analytics, bounded transaction search, and conservative
  balance-forecast APIs, plus normal Overview, Transactions, and Forecast screens
  that never read the legacy demo database.
- Added a synthetic `make demo-workspace-results` walkthrough for role-aware totals,
  expense categories, verified balances, 15/30-day paths, and forecast withholding.
- Added a synthetic 90-day chronological forecast backtest that fits only the first
  60 days and reveals the final 30 days afterward for daily and closing-balance
  comparison.

### Changed

- Reworked the Streamlit experience around plain-language navigation, a focused
  onboarding homepage, compact safety notices, responsive cards, clearer import and
  transaction wording, and a single planning-tool selector.
- Added a packaged light theme and moved technical error identities behind an
  optional details panel without weakening privacy or financial safeguards.
- Connected selectable-text digital-PDF review to file-bound spatial mapping and
  atomic confirmation, added an explicitly unconfirmed CSV download, and removed
  scanned/OCR controls from the normal interface while retaining internal tests.
- Hardened selectable-text parsing for recent UK two-digit dates and complete,
  geometry-stable accessibility labels while preserving raw cells and page-scoped
  row-accounting evidence.
- Changed normal Streamlit startup to a blank workspace instead of automatically
  loading the legacy demo database. Drafts remain gated; finalized workspace rows now
  drive the redesigned overview, transaction, and forecast pages directly.
- Added a fail-closed, versioned adapter for the supported Revolut consolidated V2
  GBP CSV structure. It reconciles adjacent balances and the footer, preserves
  physical row provenance, and discloses paired non-GBP rows behind a dedicated
  finalization confirmation.
- Extended that adapter to accept a zero-value repeated GBP section after one
  reconciled transaction table, while continuing to reject a second data-bearing or
  malformed GBP section.

### Security and privacy

- Original upload bytes, extracted PDF text, source names and hashes, and page/row
  provenance are review-time memory only and are omitted from saved workspace tables.
  Temporary workspaces clear on explicit reset/delete or local API shutdown; closing
  a browser tab by itself is not claimed as guaranteed deletion.
- Workspace responses use `no-store`, mutation bodies are capped before multipart
  parsing, loopback Host headers are allow-listed, and downloadable text is guarded
  against spreadsheet formulas.
- Both selected-workspace and all-workspace erasure require explicit confirmation.
  Developer/legacy databases, downloads, backups, and model artefacts remain separate.

## [1.0.0] - 2026-09-05

### Added

- Local-first CSV, embedded-text PDF, and scanned-PDF/OCR statement previews with
  preserved source lineage, confidence, reconciliation, and explicit review.
- Atomic confirmed CSV persistence with raw-row preservation, coverage and gap
  records, exact/probable duplicate handling, and verified balance snapshots.
- Independent transaction categories and financial roles, including reviewed
  transfer, refund, reimbursement, cash-withdrawal, and exclusion decisions.
- Coverage-aware analytics, deterministic and hybrid categorisation, recurring
  payment detection, and point-in-time-safe training datasets.
- Baseline-gated gradient-boosting forecasts, empirical uncertainty ranges,
  balance paths, anomaly review, model metadata, budgets, goals, safe-spending
  estimates, and isolated what-if scenarios.
- Selective derived-result invalidation after source changes.
- Loopback-only FastAPI and Streamlit interfaces for the implemented local user
  workflows.
- Docker/Compose packaging with local Tesseract, private named volumes, and
  read-only GitHub Actions quality and image-build checks.
- Reproducible synthetic demonstrations, full statement and branch coverage,
  privacy safeguards, release documentation, and diagrams.

### Security and privacy

- No bank credentials are required and no external OCR or model service is used.
- Statement bytes are processed locally and excluded from Git and Docker build
  contexts.
- PDF approval remains in memory and is not falsely presented as a persisted
  import.
- The unauthenticated application remains restricted to local loopback access.

### Known limitations

- Version 1 supports current/checking and savings accounts in GBP only.
- PDF extraction supports tested synthetic layouts, not every bank statement.
- PDF approval is not yet persisted atomically.
- Model results are evaluated on reproducible synthetic evidence, not a reviewed
  real-world benchmark, and are not financial advice.
- Version 1 is a local single-user release, not a remotely deployable service.

[1.0.0]: docs/releases/v1.0.0.md

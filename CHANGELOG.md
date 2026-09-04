# Changelog

All notable changes to CashFlow AI are documented here. The project follows
[Semantic Versioning](https://semver.org/), and this history describes the
repository rather than implying that a hosted service has been deployed.

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

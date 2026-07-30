# Data Contracts

Canonical transaction, import, money, date, and model-metadata contracts will be
defined in the dedicated data-contract stage.

Foundational invariants:

- income and refunds are positive;
- expenses and outgoing transfers are negative;
- stored money uses fixed precision;
- API dates use ISO 8601;
- timestamps are timezone-aware;
- raw source rows are preserved;
- malformed required values are rejected with explicit reasons.


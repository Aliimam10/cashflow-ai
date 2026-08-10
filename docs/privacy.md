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
- Preserve rejected and probable-duplicate rows locally for audit, but exclude
  them from verified calculations until an explicit later review decision.

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

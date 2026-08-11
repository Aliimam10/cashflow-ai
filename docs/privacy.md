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

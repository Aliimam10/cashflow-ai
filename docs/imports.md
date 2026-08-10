# Statement import design

CashFlow AI Version 1 will accept CSV exports, digitally generated PDF bank
statements, and scanned or camera-captured PDF statements. Implementation will
remain local-first and use one downstream validation pipeline.

## User workflow

1. Upload a supported statement and select the destination account.
2. Validate extension, MIME type, file signature, and size; calculate a hash.
3. Detect CSV, digital PDF, or image-based PDF input.
4. Extract a limited preview without persisting accepted transactions.
5. Show the original source beside provisional transaction rows where possible.
6. Highlight low-confidence fields, unbalanced totals, missing columns, and
   ambiguous debit/credit signs.
7. Allow the user to correct dates, descriptions, amounts, signs, and balances.
8. Require explicit confirmation that the extraction is accurate.
9. Pass confirmed candidates through canonical validation, cleaning, duplicate
   detection, and persistence.
10. Quarantine rejected rows with useful errors and offer an inspection export.

## Extraction routing

Digital PDFs use embedded text and table extraction first. Pages without usable
text, including camera captures and scans, use OCR. Mixed PDFs may therefore use
different extraction methods per page while presenting one combined preview.

OCR confidence is advisory rather than proof of correctness. Balance
reconciliation, row continuity, amount parsing, and user confirmation are
separate safeguards.

## Delivery sequence

1. Define canonical transactions, import candidates, provenance, confidence,
   warning, and review-status schemas.
2. Implement CSV preview and mapping as the simplest structured adapter.
3. Implement digital-PDF text/table extraction using synthetic fixtures.
4. Implement image-based PDF OCR and confidence reporting using synthetic
   scanned fixtures.
5. Build the shared side-by-side correction and confirmation workflow.
6. Feed confirmed candidates into the existing cleaning and import service.

No real bank statement will be committed as a fixture. PDF tests will be built
from synthetic transaction histories and fictional statement templates.

## Implemented CSV preview boundary

`cashflow_ai.imports.preview_csv` accepts CSV bytes and the client-supplied
filename. It performs no filesystem writes. The adapter:

- reduces path-like filenames to a safe basename and requires a `.csv` suffix;
- rejects empty files and files above the default 10 MiB limit;
- detects UTF-8, UTF-8 with a byte-order mark, UTF-16 with a byte-order mark,
  and Windows-1252;
- rejects binary control characters, duplicate or blank headings, more than 100
  columns, structurally inconsistent rows, and excessively large cells;
- detects comma, semicolon, tab, or pipe delimiters;
- validates the full file while retaining at most 25 rows in the preview; and
- suggests common date, description, amount, debit, credit, balance, identifier,
  currency, and transaction-type headings.

Failures use stable `CsvImportErrorCode` values so a later API or interface can
show useful messages without parsing exception text.

The preview includes a SHA-256 identity of the exact bytes. Confirmation must
refer to that identity; changing the file after preview invalidates approval.
`parse_csv_document` revalidates the same constraints and retains all rows in
memory for the confirmed import. Uploaded bytes are not written to disk.

## Implemented normalisation boundary

`cashflow_ai.imports.normalise_csv_row` maps one preserved CSV row into the
source-independent normaliser. It supports ISO and unambiguous UK dates, signed
amounts or debit/credit pairs, common dot/comma thousands and decimal layouts,
parenthesised/DR negatives, GBP symbols/codes, optional running balances, and
optional posting dates. It performs Unicode/whitespace cleaning and conservative
merchant cleanup while retaining every original value separately.

The result contains the normaliser name/version, derived calendar fields, an
exact source fingerprint, and a canonical matching fingerprint. Stable
`NormalisationErrorCode` values cover missing/invalid dates or amounts,
conflicting debit/credit values, unsupported currency, and invalid rows.

## Implemented duplicate safeguards

- `calculate_file_hash` and `assess_repeated_file` detect byte-identical uploads.
- `assess_transaction_duplicate` automatically skips only exact source records
  or equal non-empty bank transaction IDs on the same account.
- Equal amount, merchant similarity, and date distance of zero to two days form
  an explainable probable score. Probable matches require review.
- Different non-empty transaction IDs prevent otherwise similar purchases from
  being treated as probable duplicates.
- `assess_statement_overlap` reports inclusive same-account coverage overlap
  without claiming that all transactions in the overlap are duplicates.

## Implemented confirmed CSV import

`cashflow_ai.imports.persist_confirmed_csv` joins parsing, mapping,
normalisation, duplicate assessment, statement metadata, and local persistence
in one database transaction. It requires:

- an existing destination account whose currency matches the import plan;
- a supported CSV MIME type;
- a valid mapping for the re-parsed full document; and
- explicit, timezone-stamped confirmation of the exact preview hash.

Every source row is accounted for. Valid unique rows retain a raw record and
create a separate verified transaction. Exact duplicate rows are preserved but
skipped. Probable duplicates are preserved with their score and reasons and
remain outside verified calculations pending review. Invalid rows are preserved
with stable validation issues and no fabricated canonical fingerprint. A
byte-identical file returns its existing batch without adding a second copy.

The same unit of work records the import batch, structured statement flags,
inert note, statement coverage, explicit missing periods, and any reported
opening or closing balance snapshots. Coverage analysis distinguishes gaps that
already existed from gaps newly exposed by the incoming statement, reports date
overlap, and flags disconnected ranges. The returned `CsvImportSummary` reports
rows read, new transactions, exact and probable duplicates, rejected row
locations, repeated-file status, and coverage findings.

Any unexpected database error rolls back the complete import. There is still no
upload interface or row-review screen; those presentation layers will call this
service later. Digital-PDF and OCR adapters remain future stages and must reuse
the confirmation and preservation safeguards rather than bypassing them.

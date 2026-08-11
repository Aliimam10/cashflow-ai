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
service later. The PDF confirmation/persistence workflow remains a future stage
and must reuse the confirmation and preservation safeguards rather than
bypassing them.

## Implemented embedded-text PDF extraction

`cashflow_ai.imports.extract_text_pdf` accepts in-memory PDF bytes, a
client-supplied filename, MIME type, and destination account identity. It uses
PyMuPDF to validate and inspect the document and pdfplumber to extract tables.
The adapter:

- sanitises the filename and requires a `.pdf` suffix, `application/pdf` MIME
  type, a PDF signature, and a supported byte/page limit;
- rejects malformed and password-protected documents;
- measures embedded alphanumeric text on every page and reports the page
  numbers that require OCR instead of silently omitting them;
- preserves embedded page text in the in-memory preview and performs no
  filesystem or database writes;
- recognises common date, description, signed amount, debit, credit, balance,
  currency, identifier, and transaction-type table headings;
- removes repeated table headers and page-number rows;
- joins description-only continuation rows to the preceding transaction;
- falls back to conservative pipe-delimited or spatially separated text rows
  when no supported table is detected;
- extracts common statement-period, opening-balance, and closing-balance labels;
  and
- returns source-independent transaction drafts with exact page/record lineage,
  source and canonical fingerprints, parser identity, and structured issues.

Every PDF candidate remains `needs_review`. Invalid dates, amounts, currencies,
or row combinations keep their extracted source values but do not receive a
canonical fingerprint. Generic fallback use and missing/invalid statement
metadata are surfaced as warnings. This stage does not persist PDF rows or
accept a confirmation decision.

Support is deliberately limited to tested bordered tables and a conservative
generic text fallback. PDF layouts are not standardised, so this implementation
does not claim universal bank compatibility. Image-only, scanned,
camera-captured, and mixed PDFs containing pages without enough embedded text
are rejected with their page numbers so the caller can route them to the local
OCR adapter rather than partially importing them.

## Implemented scanned-PDF OCR extraction

`cashflow_ai.imports.extract_ocr_pdf` accepts in-memory PDF bytes and the same
filename, MIME type, account identity, size, and page constraints as the digital
adapter. It requires the open-source Tesseract executable locally and uses
pytesseract only as the Python integration layer. No statement page or OCR text
is sent to a third-party service.

For every page, the adapter:

- detects whether the original PDF lacks usable embedded text;
- renders an RGB image with PyMuPDF using a bounded DPI and pixel count;
- detects and corrects 0, 90, 180, or 270-degree orientation when Tesseract can
  determine it;
- converts the page to grayscale, improves contrast, and applies a binary
  threshold only to low-contrast pages;
- runs OCR locally and reconstructs ordered lines from recognised words;
- retains raw OCR lines plus page-level, line-level, candidate, and provisional
  field confidence;
- extracts a recognised statement period using the same coverage contract as a
  digital PDF;
- extracts recognised opening and closing balance labels for arithmetic review;
- extracts supported transaction rows through the same source-independent
  normalisation and fingerprinting boundary as CSV and digital PDF rows; and
- closes all in-memory page images after processing without creating or
  retaining application-managed temporary image files.

Invalid recognised values remain visible with structured errors and without a
fabricated canonical fingerprint. Every candidate remains `needs_review` at the
adapter boundary and no OCR transaction is persisted there.

The OCR adapter can be called for image-only statements and reports when usable
embedded text was also present. It currently OCRs every page supplied to it,
rather than silently combining extraction methods. OCR quality varies with
image resolution, focus, lighting, fonts, and layout, so the implementation
does not claim universal bank compatibility.

## Implemented statement reconciliation and review boundary

`cashflow_ai.imports.prepare_statement_review` accepts either PDF preview and
creates a targeted, in-memory review. The default OCR field-confidence threshold
is 0.85 and can be explicitly configured. Extraction errors and lower-confidence
OCR rows require a transaction-level decision; higher-confidence rows remain
visible but do not create unnecessary targeted prompts.

The service detects ambiguous slash dates and debit/credit layouts so the user
must confirm their interpretation. It calculates:

```text
expected closing balance = opening balance + signed transaction total
unexplained difference = reported closing balance - expected closing balance
```

Reconciliation is unavailable when either endpoint or any amount is missing.
Balance extraction retains the untouched amount text with its exact file/source
binding, page, line, parser and extraction provenance, and OCR confidence where
applicable. Every detected opening or closing field must be explicitly confirmed
or corrected. Those confirmed balances and their evidence remain in the approved
result even if a missing balance endpoint leaves reconciliation unavailable. A
statement period must be confirmed or corrected whenever balance evidence is
present; it is retained so later snapshots use the statement start/end date, not
an inferred transaction date. Approved transactions must fall within that period
and outside any explicit coverage gap.

Approval must match the exact file hash, resolve every uncertain row, produce
complete canonical values, and acknowledge any balance mismatch. Selecting a
date format reparses ambiguous raw transaction and posting dates; a date the user
explicitly corrected is not overwritten. Corrections cannot change the extracted
account, currency, category, or financial role.

Approved rows retain original OCR/PDF values, extracted drafts, source identities
and fingerprints, provenance, issues, confidence, and OCR line references beside
their canonical values. Rejected rows retain their full unchanged review-row
evidence rather than disappearing into a count. `approve_statement_review`
returns this trusted in-memory contract but intentionally performs no database
write; PDF persistence and the visual review UI remain later stages.

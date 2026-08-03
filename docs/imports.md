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

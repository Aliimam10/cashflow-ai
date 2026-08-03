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

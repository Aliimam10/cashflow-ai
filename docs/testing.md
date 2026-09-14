# Testing and safety gates

CashFlow AI uses focused unit tests for individual financial rules and integration
tests for complete trust-boundary workflows. Every committed fixture is fictional,
and the normal test command enforces 100% statement and branch coverage across the
Python package.

## Session-workspace safeguards

The workspace test set uses only fictional in-memory CSV and generated selectable-
text PDF content. It covers mixed and repeated uploads, automatic and explicit
per-file mappings, changed file identities and PDF structure digests, malformed and
unsupported sources, exact and probable duplicates, stale workspace/row revisions,
spreadsheet edits, penny precision, sign-compatible roles, explicit coverage and
balance confirmation, saved reload/deletion, and temporary-memory cleanup.
It also exercises a fictional multi-section consolidated V2 CSV: the GBP table must
reconcile chronologically, row-by-row, and against its footer; changed layouts fail
closed; paired non-GBP rows are counted and require explicit exclusion confirmation.

Persistence allow-list assertions prove the saved projection contains canonical
transactions, coverage/gaps, and optional balance evidence without source bytes, PDF
text, filenames, hashes, page/record provenance, mapping samples, rejected rows, or
issue evidence. Frontend tests prove startup is blank, legacy demo data is not loaded,
mapping choices begin unselected, unsupported sources can be removed, uncertain rows
cannot be bulk-approved, drafts remain gated, finalized pages use only workspace
result endpoints, and scanned/OCR controls are absent from normal navigation.

The finalized-results test set covers role-aware totals, expense-only categories,
presentation and filtering of pre-existing custom category identifiers,
complete/gapped/unknown coverage, disconnected balance evidence, revision-bound
transaction search, all four supported horizons, stale/insufficient/future/
unknown-role forecast refusals, deterministic interval reproduction, API
serialization, local-client routing, and friendly Streamlit empty/error states. It
does not claim that the normal UI can add or rename custom categories, create
workspace budgets or savings goals, or run special-case planning; those controls
remain for the next checkpoint.

## End-to-end workflows

`tests/integration/test_end_to_end_safeguards.py` exercises two readable flows:

1. A synthetic year-long CSV is previewed, explicitly confirmed, stored with raw
   lineage, categorised, assigned financial roles, checked for recurrence, analysed,
   evaluated by the leakage-safe forecasting layer, and forecast through the local
   HTTP API. The route also proves that a path-like upload name is reduced to a safe
   basename and that verified transaction responses omit raw payloads.
2. An in-memory scanned PDF passes through deterministic local OCR with a deliberate
   decimal mistake. The user corrects `-450` to `-4.50`, confirms the ambiguous date
   format, and receives a reconciled approved statement whose original evidence is
   unchanged. The test proves page images are closed, filenames are sanitised,
   sensitive input is absent from logs, and unpersisted PDF data cannot appear in
   analytics.

The first flow deliberately expects the recent-mean fallback after a first import.
Although the statement contains a year of dates, those historical outcomes only
became known at import time; treating them as known at older forecast origins would
be data leakage. Existing forecast-model tests separately evaluate the advanced
candidate with synthetic evidence that was genuinely available at each historical
cutoff.

The standard selectable-text digital-PDF route persists only through the atomic
boundary that stores balances, coverage, rejected rows, and raw lineage together.
The internal OCR regression flow deliberately ends with an in-memory approved result;
it remains excluded from normal Version 1 support and downstream analytics. It must
not be described as a persisted OCR import.

Selectable-text PDF unit tests use fictional in-memory documents to reproduce
accessibility labels embedded in transaction rows. They prove that complete repeated
label bundles with stable geometry produce clean mapped fields while untouched cells
retain the original labels. Missing, duplicated, shifted, partial, or mixed labelled
rows must remain unresolved. Separate bridge tests exercise page-scoped
cross-extractor accounting, including repeated signals, conflicting or omitted rows,
and date text adjacent to pagination; ambiguous numeric text must never be silently
discarded as a page number.

## Safeguard coverage map

| Risk or edge case | Regression evidence |
| --- | --- |
| Statement gaps and overlaps | `test_statement_coverage_analysis.py` and `test_gapped_overlapping_partial_unknown_and_unverified_coverage` |
| Ambiguous statement dates | `test_scanned_pdf_correction_preserves_evidence_and_downstream_gate` and reconciliation date-format tests |
| OCR decimal error and raw preservation | `test_scanned_pdf_correction_preserves_evidence_and_downstream_gate` |
| Savings transfer and transfer double counting | `test_matched_transfer_is_advisory_then_confirmed_atomically` and `test_account_and_consolidated_transfer_views_use_confirmed_current_pair` |
| Refund versus ordinary income | `test_refund_reimbursement_and_generic_income_remain_distinct` and `test_role_aware_totals_categories_cadence_and_largest_transactions` |
| Stale balances | `test_age_thresholds_are_inclusive_then_emit_stable_stale_warnings` |
| Missing data retained as unknown | `test_missing_evidence_is_unknown_not_zero` and `test_disconnected_coverage_marks_unknown_months_unavailable_not_zero` |
| Scenario isolation | `test_one_off_purchase_compares_paths_plans_and_preserves_database` |
| Downstream invalidation and race safety | `test_selective_invalidation_marks_current_results_stale_and_increments_revision` and `test_recomputation_fails_closed_when_relevant_source_changes_mid_run` |
| Path-like upload names | both end-to-end workflows plus the PDF adapter filename tests |
| CSV/PDF size and render limits | `test_csv_errors_have_stable_http_statuses_and_no_body_echo`, `test_empty_oversized_unsigned_and_malformed_files_are_rejected`, and `test_encrypted_page_count_and_render_size_limits_are_enforced` |
| Sensitive errors and logs | the scanned-PDF workflow, `test_request_validation_never_echoes_private_input`, `test_http_database_and_unexpected_errors_are_sanitised`, and logging field allow-list tests |
| Temporary OCR cleanup | both the scanned-PDF workflow and `test_scanned_pdf_is_rendered_preprocessed_and_converted_to_candidates` |
| Accessibility-labelled PDF rows | fictional cases in `test_spatial_pdf.py` prove stable complete bundles are projected without changing raw cells and inconsistent bundles fail closed |
| Page-scoped PDF row accounting | `test_text_pdf_spatial_bridge.py` and `test_text_pdf_extraction.py` cover omitted/conflicting signals and date-plus-pagination artefacts without accepting row counts alone |
| Mixed statement workspace | `test_workspace_service.py` covers combined fictional CSV/PDF review, mapping, source removal, deduplication, edits, and finalization |
| Saved-data minimization | `test_workspace_persistence.py` and workspace migration tests assert the explicit saved-column allow list plus selected/all-workspace deletion |
| Temporary cleanup | workspace service/API lifespan tests prove no SQLite write and memory clearing on explicit deletion or API shutdown |
| Local API workspace boundary | `test_api_security.py` and `test_workspace_api.py` cover Host rejection, pre-parser body limits, `no-store` responses, mapping limits, and privacy-safe failures |
| Blank startup, draft gating, and finalized workspace routing | `test_frontend.py`, `test_frontend_workspace_page.py`, and `test_frontend_workspace_results_page.py` prove legacy demo data is not loaded implicitly and finalized pages use workspace result endpoints |

## Manual verification

Run the readable synthetic workspace walkthrough:

```bash
make demo-workspace
```

Expected output:

```text
CashFlow AI synthetic statement-workspace check
mixed sources accepted: 2
combined canonical rows: 3
saved rows restored: 3
original CSV/PDF bytes persisted: no
temporary workspace persisted: no
temporary workspace in memory after API-stop cleanup: no
```

The command creates no real upload, filesystem database, or retained PDF. Safe
parameters to vary are fictional dates, descriptions, and exact two-decimal amounts
inside `src/cashflow_ai/workspaces/demo.py`. Do not paste real financial data into a
demo, fixture, assertion failure, screenshot, or coverage artefact.

Run the read-only analytics and forecast walkthrough:

```bash
make demo-workspace-results
```

Expected output:

```text
CashFlow AI synthetic workspace-results check
coverage: complete (90/90 days)
income: £4,500.00
spending: £2,760.00
net transfers: -£200.00
latest verified balance: £2,560.00
expense categories: Rent/Housing=£1,800.00, Groceries=£960.00
15-day forecast: available; expected £2,937.50; range £1,789.09 to £4,439.24
30-day forecast: available; expected £3,132.50; range £1,322.67 to £5,378.25
unresolved-role guard: withheld (unknown_financial_roles)
files, databases, and real financial data created: no
```

It performs no file or database write. Safe parameters to vary are only fictional
dates, categories, and two-decimal values in
`src/cashflow_ai/workspaces/results_demo.py`.

Run the inspectable 60/30 chronological holdout:

```bash
make demo-workspace-backtest
```

It creates three Git-ignored, fictional consolidated-V2-style CSVs: the complete
90-day source, its 60-day training split, and its 30-day evaluation split. The
forecast sees only the first split. Expected closing balances are £2,839.56 forecast
and £2,848.00 actual, with £9.39 daily MAE and 28/30 actual days inside the nominal
80% interval. Vary only the fictional patterns in
`src/cashflow_ai/workspaces/forecast_backtest_demo.py`; these fixed results are a
regression check, not an accuracy promise.

The older cross-boundary checks remain available:

Run only the two synthetic cross-boundary workflows:

```bash
make test-safeguards
```

Expected result: two named tests pass. No bank statement, database, upload, page image,
or model artefact remains in the repository. To inspect one path at a time, use the
safe `-k` selector:

```bash
uv run pytest -o addopts="-ra --strict-config --strict-markers" -vv \
  tests/integration/test_end_to_end_safeguards.py -k csv_to_forecast
uv run pytest -o addopts="-ra --strict-config --strict-markers" -vv \
  tests/integration/test_end_to_end_safeguards.py -k scanned_pdf
```

The complete release gate remains:

```bash
make format
make check
make pre-commit
make check-import
```

For a focused, fictional-only check of the spatial and row-accounting boundary, run:

```bash
uv run pytest -q --no-cov tests/unit/test_spatial_pdf.py \
  tests/unit/test_text_pdf_spatial_bridge.py tests/unit/test_text_pdf_extraction.py
```

Expected result: all selected tests pass without creating an upload, database, or
statement artefact.

`make check` must finish with 100% statement and branch coverage. Do not bypass a
failed privacy assertion or reduce the configured coverage threshold.

## Container delivery safeguards

The four synthetic/static checks in `tests/integration/test_container_delivery.py`
verify that the image is non-root and includes Tesseract, private paths cannot enter
the Docker context, Compose retains loopback-only networking and private volumes, and
continuous integration has read-only permissions plus every required quality gate.
Run them directly with:

```bash
make test-containers
```

Expected result: four tests pass without creating a database, model, upload, image,
or container. `make docker-config` additionally asks Docker Compose to resolve the
real configuration, and GitHub Actions performs the Linux image build, packaged
import check, and Tesseract executable check. See `docs/containers.md` for a complete
manual start/readiness/stop procedure.

## Release-documentation safeguards

`tests/integration/test_release_documentation.py` keeps the package, project,
lockfile, changelog, and release-note version aligned; checks that required limitations
and financial interpretations are stated; and resolves local links in the release
entry points. These tests prevent a documentation-only release edit from silently
claiming PDF persistence, real-world model accuracy, or a missing document.

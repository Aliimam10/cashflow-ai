# Testing and safety gates

CashFlow AI uses focused unit tests for individual financial rules and integration
tests for complete trust-boundary workflows. Every committed fixture is fictional,
and the normal test command enforces 100% statement and branch coverage across the
Python package.

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

PDF approval currently creates trusted in-memory rows, not a database import. This is
an intentional atomicity and privacy boundary: balances, coverage, rejected rows, and
raw lineage must eventually be persisted together. Until that unit of work exists,
the integration test requires downstream analytics to report missing data rather
than consume the OCR result. It must not be described as a persisted PDF import.

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

## Manual verification

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

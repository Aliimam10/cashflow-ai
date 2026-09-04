# Version 1 user guide

CashFlow AI is a local decision-support application for reviewing statement data,
understanding observed cash flow, and exploring cautious forecasts, budgets, goals,
and scenarios. It does not connect to a bank, move money, or provide financial
advice.

## Supported scope

| Area | Version 1 support |
| --- | --- |
| Accounts | Current/checking and savings |
| Currency | GBP; accounts of different currencies cannot be combined |
| CSV | Preview, mapping, explicit confirmation, and atomic persistence |
| Digital PDF | Embedded-text/table extraction, correction, reconciliation, and in-memory approval |
| Scanned or camera PDF | Local Tesseract OCR, confidence review, correction, reconciliation, and in-memory approval |
| Credit cards, loans, investments | Not supported as account types |
| Bank connection or credentials | Not used |
| Remote or multi-user access | Not supported |

Create one destination account for each real account represented by a statement.
Never combine a current account and savings account during import merely because
they belong to the same person. Consolidated analytics may combine selected accounts
only when ownership and currency match.

## Start the application

With the uv environment:

```bash
make setup
make db-upgrade
make api
```

Keep the API running, open a second terminal, and run:

```bash
make ui
```

Open `http://127.0.0.1:8501`. Docker users can instead follow
[`containers.md`](containers.md). Both methods are local-only.

## Import a statement

1. Create the local profile and choose GBP plus an IANA timezone.
2. Create a current/checking or savings account without entering account numbers,
   login details, or bank credentials.
3. Open **Import statements**, select that destination account, and choose CSV,
   digital PDF, or scanned PDF deliberately.
4. Inspect the source preview. Confirm column mappings for CSV or extracted fields,
   dates, debit/credit signs, confidence, and balances for PDF.
5. Describe the actual statement period as complete, gapped, partial, or unknown.
   Record every known missing date interval rather than filling it with zeros.
6. Add an optional note only as reference context. Notes never change categories,
   roles, analytics, or forecasts.
7. Correct extraction errors while leaving the displayed original evidence intact.
8. Resolve each uncertain row and acknowledge any reconciliation difference.
9. Confirm the exact file only after the preview is correct.

Confirmed CSV rows are persisted atomically with their raw source rows, verified
transactions, statement context, coverage, and balance evidence. Exact duplicates
are skipped; probable duplicates remain outside calculations until reviewed; invalid
rows are retained with errors.

PDF confirmation currently returns a trusted in-memory approval and **does not save
the statement or its transactions**. This is an intentional Version 1 limitation,
not a successful PDF import. Do not re-enter an approved PDF balance manually as a
substitute for the missing atomic PDF persistence workflow.

## OCR limitations and review

Tesseract runs locally, but OCR accuracy depends on resolution, focus, perspective,
rotation, lighting, compression, fonts, table borders, and the statement layout.
Common failure modes include:

- a decimal point being omitted, such as `4.50` becoming `450`;
- `0`, `O`, `1`, `I`, and `l` being confused;
- a minus sign or debit/credit column being missed;
- day and month order being ambiguous;
- wrapped descriptions attaching to the wrong transaction; and
- rows, balance labels, or page boundaries being skipped.

Confidence is a review signal, not proof. Compare each amount and date with the
visible original, confirm the date convention and sign interpretation, and check:

```text
opening balance + signed transactions = closing balance
```

An unavailable or mismatched reconciliation requires more review; it must never be
silently treated as balanced. Only tested synthetic layouts are supported, and an
apparently successful extraction can still be wrong.

## Statement gaps, overlaps, and freshness

Coverage describes which dates the statement can actually establish:

- `complete`: the entire confirmed period is represented;
- `gapped`: explicit internal ranges are missing;
- `partial`: the document is known not to cover the full intended period;
- `overlapping`: verified coverage overlaps an earlier statement; and
- `unknown`: continuity cannot be established.

A covered date with no eligible transaction can be a genuine zero. An uncovered
date is unknown. Analytics label incomplete totals as observed-only and withhold
rates that would imply completeness. Forecasting requires consecutive coverage and
fresh transaction/balance evidence; stale or insufficient data causes warnings,
wider intervals, a baseline fallback, or archive mode.

Uploading old history today does not create a trustworthy historical backtest as if
the application had known it months ago. Evidence becomes available at its recorded
verification time and is never backdated for model evaluation.

## Categories and financial roles

Category answers **what was it for?**—for example housing, groceries, utilities,
transport, health, education, subscriptions, or travel. Financial role answers
**how should it count?**—income, expense, transfer, refund, reimbursement, cash
withdrawal, excluded, or unresolved.

- A matched transfer between the user's accounts moves money but does not create
  consolidated income or expense.
- A refund reverses spending and is reported separately from salary income.
- A reimbursement is a positive recovery associated with an earlier cost and is
  also distinct from salary.
- A cash withdrawal is visible, but the later use of the cash is unknown unless the
  user has separate evidence.
- An unresolved role remains out of confident headline calculations until reviewed.

Suggestions are advisory. Review the evidence and explicitly confirm or reject a
transfer/refund suggestion. Correct a category when rules or the optional local
classifier are wrong. A correction becomes auditable feedback; it does not trigger
background retraining.

## Analytics, forecasting, and planning

The transactions dashboard can filter verified activity, review probable duplicates
and roles, correct categories, and show coverage-aware totals and category breakdowns.
The forecasting and planning screen can:

- review and confirm recurring payments;
- compare the advanced forecast candidate with simple baselines;
- show a daily expected balance with an empirical likely range;
- track monthly category and weekly discretionary budgets;
- track a savings target or minimum-balance goal;
- estimate conservative safe weekly spending; and
- compare isolated one-off or recurring what-if scenarios.

Forecast intervals are empirical estimates, not guarantees. Longer paths reuse
earlier predictions, so error can compound. Scenario results are hypothetical and
never alter imported transactions or the baseline forecast. Unusual-activity results
mean **Needs review**, **Possible duplicate**, or **Unusual**—never confirmed fraud.

## Reproducible fictional demonstration

Run the release walkthrough without retaining a database:

```bash
make demo-api
make demo-recurrence
make demo-forecast-model
make demo-forecast-path
make demo-anomalies
make demo-planning
make demo-scenario
```

Expected highlights include `raw source payload returned: false`,
`temporary database retained: false`, an explicitly selected forecast model or
baseline, a 30-day likely balance range, protected known recurrence, and a scenario
marked `hypothetical: true`. All values and identities are fictional. Safe parameters
and fallback commands are recorded in the module documents linked from the main
[`README`](../README.md).

## Stop and retain data safely

Stop direct processes with `Ctrl-C`. `make docker-down` stops containers while
retaining named volumes. SQLite files, statements, screenshots containing financial
details, generated exports, and model artefacts are private local data: do not commit,
email, or upload them. See [`privacy.md`](privacy.md) before using non-synthetic data.

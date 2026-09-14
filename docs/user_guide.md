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
| CSV | Mixed-workspace review, mapping, editing, and final canonical persistence |
| Digital PDF | Selectable-text extraction, mapping, editing, and final canonical persistence |
| Workspace retention | Saved minimized canonical table, or temporary API-process memory |
| Scanned or camera PDF | Not shown or supported in the normal Version 1 interface |
| Credit cards, loans, investments | Not supported as account types |
| Bank connection or credentials | Not used |
| Remote or multi-user access | Not supported |

Create one workspace/account label for each real account represented by a statement.
Never combine a current account and savings account merely because they belong to the
same person. The older developer import APIs still use persisted profile/account
records; normal workspace startup does not create or load them.

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

After switching branches or changing API code, stop both running commands with
`Ctrl+C` and start them again. A process that was already running cannot discover a
new route such as the statement-workspace endpoint. The interface reports this case
as an outdated local API and prompts you to rerun `make api`.

## Build a statement workspace

The normal interface starts blank. It does not load the legacy demo database or show
old transactions until you deliberately choose **Resume saved workspace**.

1. Open **Bank statements** and choose a retention mode. **Saved on this device** is
   the default; **Temporary** remains only in the local API process memory.
2. Give the workspace a short account label. Every file in one workspace must belong
   to the same GBP personal current or savings account.
3. Select one or more CSV exports and selectable-text digital PDFs together, then
   choose **Review selected files**.
   A supported Revolut consolidated V2 export may contain report material before
   the GBP table. The app locates that exact layout automatically; a changed layout
   is rejected rather than guessed.
4. For any ambiguous file, inspect the bounded sample, map the date, description,
   signed amount or debit/credit columns, and optional running balance, then re-select
   that exact file. Remove unsupported files or obtain a CSV export.
5. Review the combined spreadsheet. Correct dates, descriptions, signed penny-precise
   amounts, running balances, categories, and financial roles. Mark each row
   **Include** or **Exclude**. The bulk include option applies only to clean rows; it
   never approves an uncertain or probable-duplicate row.
6. Check every probable duplicate and choose whether it is a separate transaction or
   should be rejected. Deterministic exact duplicates are removed automatically.
7. Confirm the combined statement coverage. Record each known gap explicitly; an
   uncovered date remains unknown rather than becoming zero spending.
8. Check the rows against the source statements, confirm the day/month date
   interpretation and positive-in/negative-out sign convention, and confirm any
   displayed running-balance evidence. If a consolidated export also contains a
   paired non-GBP transaction section, check the displayed excluded-row count and
   explicitly confirm that only GBP should enter this workspace.
9. Finalize the canonical table. Choose **View your overview**, or use the left
   navigation to open the approved transactions and forecast.

Source filenames, hashes, page/row provenance, upload bytes, and extracted PDF text
exist only while a draft is reviewed. In saved mode, SQLite receives only the final
included canonical rows, confirmed coverage, and optional latest balance. Rejected
rows and the original unedited PDF/CSV payload are not retained in this minimized
saved projection. Temporary mode writes none of the workspace to SQLite.

**Start a blank workspace** deletes a draft or temporary workspace after confirmation.
For a finalized saved workspace it only leaves the selection; the saved table remains
available until **Delete workspace data** is explicitly confirmed. Both delete actions
affect only the selected workspace. From the blank start screen, **Privacy and
deletion → Delete all local workspace data** erases every active and saved statement
workspace after a second explicit confirmation. Neither control removes legacy
developer data, downloaded CSV files, backups, or model artefacts.

For the supported consolidated CSV layout, every adjacent GBP running balance and
the table's signed footer total must reconcile exactly before rows are shown. The
original bank category is kept as source metadata only. It is not trusted as the
app's category or financial role, so the user still reviews those fields. Summary,
interest, and paired foreign-currency sections never become hidden GBP transactions.

Temporary state is cleared by explicit start-over/deletion or when the local API
process stops. Closing only the browser tab is not guaranteed to notify the server,
so stop the API if immediate memory cleanup matters.

The lower-level, legacy per-file **PDF confirmation** API remains under regression
coverage and preserves full raw lineage. The normal workspace uses its extraction and
validation safeguards but saves only the minimized finalized canonical projection
described above.

## Unsupported scans and internal OCR

Scanned, photographed, image-only, and mixed-text PDFs are not accepted by the normal
Version 1 workflow. The app recommends a bank-exported CSV because OCR can lose a
decimal point, sign, row, or page boundary. An internal local Tesseract adapter remains
only for regression testing; it is not exposed as a supported upload choice.

Potential OCR failure modes include:

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

Draft workspaces keep **Overview**, **Transactions**, and **Forecast & plans** gated.
After finalization, the normal UI uses only that approved workspace and never falls
back to old demo balances:

- **Overview** shows the latest verified account balance, gap-preserving pulse line,
  observed income and expenses, transfers separately, expense-only category totals,
  monthly cash flow, statement coverage, and recent activity.
- **Transactions** shows up to the newest 100 approved canonical rows. It is read-only
  because finalization freezes the reviewed table; start a new workspace to make a
  corrected version.
- **Forecast & plans** currently provides 15-, 30-, 60-, and 90-day balance horizons.
  If financial roles, recent continuous coverage, 60 days of history, or a fresh
  latest balance are missing, it explains the exact issue and draws no future graph.

The new workspace forecast uses a reproducible recent 60-day weekday cash-flow
baseline plus an empirical interval. It does not claim that the older advanced model
won a historical test: all rows in a one-shot workspace became known together when
the table was finalized. Budget controls, savings-goal progress, and deterministic
special-case recommendations remain the next workspace checkpoint.

Forecast intervals are empirical estimates, not guarantees. Longer paths reuse
earlier predictions, so error can compound. Scenario results are hypothetical and
never alter imported transactions or the baseline forecast. Unusual-activity results
mean **Needs review**, **Possible duplicate**, or **Unusual**—never confirmed fraud.

## Reproducible fictional demonstration

First verify the new statement workspace:

```bash
make demo-workspace
```

Expected output is:

```text
CashFlow AI synthetic statement-workspace check
mixed sources accepted: 2
combined canonical rows: 3
saved rows restored: 3
original CSV/PDF bytes persisted: no
temporary workspace persisted: no
temporary workspace in memory after API-stop cleanup: no
```

This uses one in-memory fictional CSV and one in-memory selectable-text PDF. Safe
parameters to vary are only the synthetic dates, descriptions, and penny-precise
amounts in `src/cashflow_ai/workspaces/demo.py`.

Then verify the exact analytics/forecast boundary without a database or file:

```bash
make demo-workspace-results
```

Expected output includes complete `90/90` synthetic coverage, income and expense
totals, transfers kept separate, two expense categories, available 15- and 30-day
paths with ranges, and `unresolved-role guard: withheld
(unknown_financial_roles)`. Safe values to vary are only the fictional dates and
two-decimal amounts in `src/cashflow_ai/workspaces/results_demo.py`.

To see a forecast compared with outcomes it was not allowed to inspect, run:

```bash
make demo-workspace-backtest
```

This produces Git-ignored fictional 90-, 60-, and 30-day consolidated-layout CSVs.
The real workspace importer finalizes the first 60 days, the app forecasts the next
30, and only then does the demo reveal those 30 actual days for comparison. Expected
final error is -£8.44 (forecast minus actual). This demonstrates chronological
separation; it does not validate future accuracy for a real account.

The older developer demonstrations remain available:

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

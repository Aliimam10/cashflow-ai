# Coverage-aware forecasting data and baselines

Commit 22 creates trustworthy inputs and simple references, not an ML forecast. The
daily calendar uses the intersection of verified coverage across selected accounts.
Covered dates with no eligible transactions are zero; uncovered dates and dates with
an unresolved financial role remain null.

Weekly targets require seven covered days. Confirmed recurring expenses are
separated only after their confirmation evidence is available. Features require
eight immediately preceding weeks, so a gap breaks lags until sufficient consecutive
history returns. Features include lags, rolling means, payday distances, month, ISO
week, and known recurring outflow.

## Point-in-time availability

The dataset plan supplies an explicit timezone-aware knowledge cutoff. A covered
daily observation records `known_at`, the latest time needed to establish its
statement coverage, full UTC calendar day, trusted import lineage, transaction
verification, role decision, and recurrence membership. A partial current day stays
unknown even if its statement range already claims coverage. A weekly target takes
the latest availability of its seven complete days, and a
feature row retains both its Monday `forecast_origin_at` and the target's
`target_known_at`. A historical value can be used as a lag or training outcome only
when it was available strictly before that forecast origin. This prevents a later
role correction, statement import, recurrence confirmation, or member refresh from
appearing in an earlier prediction.

Eligible transactions must retain one-account lineage from the selected owned
account through the verified row, confirmed raw row, and import batch. Raw and batch
source types must agree. Confirmed CSV rows may come from verified or
`needs_review` batches because that batch state can represent retained row-level
issues; digital-PDF and OCR-PDF rows require a verified batch. Future imports,
unconfirmed raw rows, source mismatches, and cross-account links are excluded.

A confirmed recurrence schedule is anchored only to trusted members whose complete
evidence existed by the user's confirmation time. A later identified, older-dated
member can remove that observed transaction from discretionary spending from its
own `identified_at` time onward, but cannot move the already confirmed schedule in a
historical fold. The dataset also records a cutoff-bound recurring-outflow amount
for the week immediately after its latest complete target.

This makes a practical limitation visible: uploading a year-old statement today does
not prove that CashFlow AI knew those values throughout that year. Its historical
weeks have today's availability, so the service will not backdate them to manufacture
a June-to-August backtest. They can support future forecasts once the required eight
consecutive outcomes are known before a future Monday; a genuine historical backtest
requires contemporaneous imports/audits or another trustworthy timestamped snapshot.

## Manual verification

Run:

```bash
make demo-forecast
```

Expected output includes `weekly targets: 20`, `leakage-safe feature rows: 12`,
`final test weeks: 3`, and MAE for five baselines. Each baseline is evaluated as a
rolling one-week-ahead prediction: after a test week's outcome becomes available it
may enter the next week's history. Commit 23 must give the primary candidate the same
information at each origin.

To demonstrate a missing statement week:

```bash
uv run cashflow-forecast-demo --weeks 24 --test-weeks 3 --gap-week 10
```

Expected output includes `gap retained`. You may safely vary `--weeks` (minimum 13),
`--test-weeks` while leaving eight lag weeks plus training and validation, and
`--gap-week` from zero through `weeks - 1`; invalid combinations fail with a readable
argument error. All inputs are synthetic.

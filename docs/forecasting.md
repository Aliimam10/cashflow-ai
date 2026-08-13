# Coverage-aware forecasting data and baselines

Commit 22 creates trustworthy inputs, not an ML forecast. The daily calendar uses
the intersection of verified coverage across selected accounts. Covered dates with
no eligible transactions are zero; uncovered dates remain null.

Weekly targets require seven covered days. Confirmed recurring expenses are
separated. Features require eight immediately preceding weeks, so a gap breaks lags
until sufficient consecutive history returns. Rows must be verified by the explicit
knowledge cutoff. Features include lags, rolling means, payday distances, month,
ISO week, and confirmed recurring outflow.

## Manual verification

Run:

```bash
make demo-forecast
```

Expected output includes `weekly targets: 20`, `leakage-safe feature rows: 12`,
`final test weeks: 3`, and MAE for five baselines.

To demonstrate a missing statement week:

```bash
uv run cashflow-forecast-demo --weeks 24 --test-weeks 3 --gap-week 10
```

Expected output includes `gap retained`. You may safely vary `--weeks` (minimum 12),
`--test-weeks`, and `--gap-week`; all inputs are synthetic.

# Budgets, goals, and safe-spending estimates

Commit 27 adds a local deterministic planning service. It stores explicit user
budgets and goals, reads only verified transaction/coverage evidence through the
analytics boundary, consumes a data-minimised balance-forecast summary, and returns
budget progress, required savings contributions, projected shortfalls, and a
conservative weekly spending estimate.

This is a Python service rather than an API or UI. Its outputs are estimates based on
the supplied evidence and are not financial advice or guarantees.

## Persisted planning types

A monthly category budget covers exactly the first through last day of one calendar
month and requires one active category. A weekly discretionary budget covers Monday
through Sunday and has no category. In this stage “discretionary” means verified
expense-role spending not linked to a confirmed recurring payment. It does not claim
that every such purchase was optional.

A savings target records a positive target, already saved amount, and future target
date. A minimum-balance goal records one positive floor per account and has neither a
target date nor a saved amount. Creation validates local profile/account ownership,
active state, GBP, period shape, and category state. Duplicate scopes fail rather
than silently replacing a user plan.

Migration `0008` maps old budget rows to `monthly_category` and old goal rows to
`savings_target` without changing their values. It makes category nullable only for
weekly budgets and adds one-per-period/one-floor database constraints. Downgrade is
permitted when only legacy-compatible types remain; it refuses to erase weekly or
minimum-balance records that the older schema cannot represent.

## Coverage-aware budget progress

Every budget result contains the exact inclusive observation period plus its complete,
partial, or missing `DataCoverageIndicator`. Amount used comes only from verified
expense-role transactions. Category budgets select their category; weekly budgets
exclude confirmed recurring expenses.

When every elapsed date is covered, projected use is a transparent run-rate estimate:

```text
projected use = amount used × full period days ÷ elapsed covered days
```

The result reports remaining amount and any projected overrun. When elapsed coverage
is partial or missing, projected use and overrun are unavailable. Observed partial
spending may remain visible, but it is never scaled as though missing dates were zero.

## Savings contributions

Remaining savings is the positive difference between target and current saved amount.
Contribution months include the current and target calendar months. Required monthly
contribution divides the remaining amount by that count and rounds upward to the
nearest penny. An overdue target requires the whole remaining amount immediately; a
conservatively retained legacy target without a date does the same and carries a
specific warning.

Minimum-balance progress compares the goal with the lowest lower-bound balance across
the forecast horizon, not only its expected final balance. Any difference becomes a
`minimum_balance_shortfall` warning.

## Safe weekly spending

The forecast adapter removes daily simulations and retains only account, period,
currency, lowest lower balance, final balance bounds, total expected discretionary
spending, and controlled forecast warnings. All selected account summaries must cover
the same future period.

The deterministic calculation is:

```text
horizon weeks = forecast days ÷ 7
expected weekly spending = forecast discretionary total ÷ horizon weeks
lower balance headroom = Σ(lowest lower balance − user floor)
required weekly savings = required monthly savings × 12 ÷ 52

cash-based weekly limit = max(
    0,
    expected weekly spending
    + lower balance headroom ÷ horizon weeks
    − required weekly savings,
)

safe weekly spending = min(
    cash-based weekly limit,
    first forecast week's explicit weekly budget when present,
)
```

Without an explicit minimum-balance goal, the conservative default floor is zero.
The amount is rounded downward to avoid overstating capacity. The result identifies
whether cash headroom, the weekly budget, both, or no remaining headroom set the
limit. Forecast limitations, incomplete coverage, budget overruns, floor breaches,
overdue goals, and contribution shortfalls remain structured warnings.

## Manual verification

Run the fixed fictional example:

```bash
make demo-planning
```

Expected key output:

```text
transaction coverage: 2026-08-01 to 2026-08-14 (complete)
food budget used: GBP 100.00
food projected month use: 221.43
required monthly savings contribution: GBP 120.00
safe weekly spending estimate: GBP 47.30
financial advice guarantee: false
```

Verify that missing days are not silently projected:

```bash
uv run cashflow-planning-demo --incomplete-coverage
```

Coverage becomes `partial` and projected month use becomes `unavailable`. You may
also test a stressed forecast explicitly:

```bash
uv run cashflow-planning-demo --forecast-low-balance 200
```

The safe weekly estimate becomes `GBP 0.00` and a savings-contribution shortfall is
reported. Increasing the numeric value increases headroom, but the explicit £60
fictional weekly budget can still cap the result. The demo uses only an in-memory
database and fictional records.

## Current limitations

- Budget projection uses an explicit run rate rather than a category-level forecast.
- Savings targets are not automatically linked to real transfers or account balances.
- The safe amount consumes an existing forecast; it does not train or select a model.
- Multiple savings goals share aggregate available capacity because priority is not
  part of the authoritative Commit 27 scope.
- Updates, deletion, API routes, and UI controls remain later interface work.

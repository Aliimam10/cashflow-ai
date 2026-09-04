# Isolated financial scenarios

Commit 28 adds a local what-if comparison service. It generates the unchanged
baseline forecast first, compiles one typed user scenario into temporary dated cash
adjustments, builds a separate scenario forecast from the same evidence and model,
and compares their planning results. Every result is marked `hypothetical: true`.

Scenario evaluation does not insert a `scenarios` row, transaction, recurring-payment
record, forecast run, or planning record. A comparison can therefore be discarded
without restoring any financial data.

## Supported scenario meanings

All entered amounts are positive magnitudes. The service applies the repository's
sign convention internally.

| Scenario | Meaning in the forecast |
| --- | --- |
| One-off purchase | One dated cash outflow |
| Travel expense | One dated cash outflow |
| New subscription | Repeated outflow at the selected frequency |
| Cancelled subscription | Offsets only future occurrences of one confirmed baseline candidate |
| Rent increase | Repeated additional outflow |
| Income increase | Repeated additional inflow |
| Income reduction | Repeated cash reduction |
| Category-spending reduction | Repeated avoided expense for one controlled category |
| New savings transfer | Repeated current-account outflow reserved for savings |

Weekly, fortnightly, monthly, quarterly, and annual recurrence are supported. Calendar
recurrence preserves a month-end date where possible. A recurring scenario ends at
its optional end date or the forecast horizon. All scenario dates must remain inside
that horizon.

A cancelled-subscription request supplies a confirmed recurring-payment candidate
identity instead of trusting a user-entered amount. Only matching negative expense
occurrences already present in the baseline are neutralised; an income candidate
cannot be cancelled through this scenario type. The underlying candidate remains
unchanged.

Category reductions use an explicit amount per recurrence rather than an unsupported
percentage or a fabricated category forecast. They improve the cash path as avoided
spending; they are not reported as income.

## Comparison output

`FinancialScenarioComparison` contains:

- the complete unchanged baseline balance path;
- the compiled temporary `ForecastScenario` overlay;
- the separate scenario balance path;
- expected ending-balance and cautious lowest-balance differences;
- baseline and scenario Commit 27 planning results;
- coverage-aware projected-use changes for active budgets;
- minimum-balance and savings-capacity goal effects;
- the change in conservative safe weekly spending; and
- inherited interval method, probability, widening, performance, and warnings.

One-off purchases, travel expenses, and category reductions affect a matching weekly
non-recurring budget. Expense additions or reductions affect a monthly category
budget only when the scenario explicitly names that category. Income and savings
transfers never become expense-budget use. When baseline transaction coverage is
partial or missing, both baseline and scenario budget projections remain unavailable.

Minimum-balance effects compare the two lower forecast paths. Savings targets retain
their deterministic required contribution, while the comparison reports whether
available forecast capacity moves into or out of the aggregate savings-shortfall
state.

## Uncertainty and baseline warnings

The baseline and scenario use the same trained model, empirical residuals, random
seed, confirmed recurring evidence, interval probability, and widening rules. A
dated deterministic adjustment shifts simulated balances without narrowing the
baseline uncertainty. The service fails closed if interval widths or uncertainty
metadata unexpectedly change.

`baseline_forecast_limitation` is returned when the underlying forecast already uses
a low-confidence model, limited residual history, or stale evidence.
`incomplete_baseline_coverage` is returned when an active budget lacks complete
elapsed statement coverage. Scenario results never conceal either limitation.

## Manual verification

Run the default fictional £250 one-off food purchase:

```bash
make demo-scenario
```

Expected key output:

```text
scenario type: one_off_purchase
end balance difference: GBP -250.00
food budget projected-use difference: GBP 250.00
uncertainty inherited: true
warnings: none
hypothetical: true
```

Try a fictional monthly £100 income increase:

```bash
uv run cashflow-scenario-demo --scenario-type income_increase --amount 100
```

The ending-balance difference is positive and three temporary monthly changes are
generated inside the 90-day demo horizon. You may replace the type with any value
listed by `uv run cashflow-scenario-demo --help` and vary the amount above zero.
For `cancelled_subscription`, the amount argument is ignored because the fictional
confirmed baseline occurrence supplies the amount.

## Current limitations

- One comparison targets one account because the current forecast-path service is
  account-scoped.
- Travel is represented as one total dated outflow rather than a daily itinerary.
- Category reduction is an explicit avoided amount, not a learned category forecast.
- Savings-goal risk is an aggregate capacity check, not a probability of success.
- Scenarios can be built and compared in the local Commit 36 UI, but they are not
  saved, edited after comparison, or shared.
- Outputs are estimates, not guarantees or financial advice.

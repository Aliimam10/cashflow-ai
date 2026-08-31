# Coverage-aware analytics

CashFlow AI currently exposes deterministic analytics as a local Python service.
It does not expose an HTTP endpoint or user interface and does not persist a
report. Callers supply an owned account scope and an inclusive date range; the
service returns immutable typed contracts.

## Trusted inputs

Transaction totals read accepted `verified_transactions` records and their
current financial roles. A pending suggestion cannot change a calculation.
Statement notes and flags are never queried. Categories are optional labels and
do not decide cash-flow meaning.

Coverage is trusted only from a verified import batch:

- `complete` and `overlapping` contribute their entire inclusive range;
- `gapped` contributes the known ranges left after every explicit gap is removed;
- `partial` and `unknown` contribute no proven dates; and
- unverified or needs-review batches contribute no coverage.

For a consolidated request, fully covered dates are the intersection of every
selected account's known ranges. Partially covered dates are the union minus that
intersection. Dates outside the union are missing. A transaction-free date never
proves coverage by itself.

## Role-aware formulas

All arithmetic uses fixed-precision `Decimal` values. Amount signs follow the
canonical contract: inflows are positive and outflows are negative.

```text
total income          = sum(positive income-role amounts)
total expenses        = magnitude of negative expense-role amounts
total refunds         = sum(positive refund-role amounts)
total reimbursements  = sum(positive reimbursement-role amounts)
cash withdrawals      = magnitude of negative cash-withdrawal amounts

net cash flow = income + refunds + reimbursements
                - expenses - cash withdrawals
```

Transfer-in, transfer-out, unknown, and excluded amounts never enter external net
cash flow. Unknown and excluded inflows/outflows are reported separately rather
than silently discarded. Cash withdrawals remain separate from expenses because
withdrawing cash does not prove how the cash was later spent.

The savings-rate formula is:

```text
savings rate = net cash flow / total income * 100
```

It is rounded to two decimal places using decimal half-up rounding. The service
withholds it when coverage is incomplete, any observed role remains `unknown`, or
total income is zero. An explicitly excluded row is a resolved user decision and
does not block the rate.

## Transfers

In account view, transfer inflow, outflow, and net movement remain visible without
being called income or expense. In consolidated view, a paired transfer is hidden
from transfer movement when both accounts are selected, even if its two legs fall
in different months.

A confirmed suggestion is considered a current pair only when both stored roles
still match the suggestion, the roles are opposite transfer directions, the
accounts differ, currencies match, and amounts are exact opposites. If one leg is
later overridden, the stale suggestion no longer suppresses movement. A one-sided
or boundary transfer remains visible but never enters headline income or expense.

## Complete, observed, and unavailable values

A complete requested period receives the `complete_period` basis. If some trusted
coverage exists but the period is incomplete, returned totals contain observed
transactions only and carry the `observed_only` basis. They are not estimates for
the missing dates.

When no selected account has trusted coverage, totals, category spending, and
cadence spending are unavailable rather than zero. The observed transaction count
is still returned so trusted rows are not silently hidden. This distinction also
applies month by month:

- a fully covered month with no transactions has real zero totals;
- an uncovered month has no totals; and
- a partially covered month contains labelled observed-only totals.

Monthly changes are returned only between adjacent full calendar months when both
are completely covered and neither contains an unknown role. Otherwise the
comparison states whether a partial calendar month, incomplete coverage, or an
unresolved role made comparison unsafe.

## Categories, recurrence, and largest transactions

Category spending includes expense-role transactions only. A null category is an
explicit uncategorised bucket, not the `other` category. Refunds, transfers, and
cash withdrawals do not become category expenses merely because of category
metadata.

The deterministic categorisation service can now populate those expense
categories from an explicit transaction decision, scoped personal rule, exact
merchant mapping, whole-phrase keyword rule, or `needs_review` fallback. A later
analytics call will then include the assigned category in its breakdown. Category
assignment still cannot change headline totals because those depend exclusively
on the independently confirmed financial role. Transactions that have not run
through categorisation can still retain a null category.

Commit 21 owns recurrence detection and confirmation. The current database has no
reliable transaction-to-recurring-series link, so all expense-role spending is
reported as `unclassified`; the service does not infer recurrence from merchant,
description, direct-debit text, or frequency.

Largest transactions are sorted by absolute amount, then date, account, and
transaction ID. Their signed amount and role remain visible for interpretation.
Rows explicitly excluded from analytics are omitted.

## Balance history

Balance snapshots are evidence, not transactions. The service reads verified
snapshots only and chooses one per account/date using manual, statement closing,
running balance, then statement opening priority, with recording time and ID as
remaining tie-breakers.

Points inside one proven coverage range form one chart-safe segment. An explicit
coverage gap starts a new segment. A balance point outside proven coverage is a
standalone segment. Callers must not interpolate between segments or reconstruct
missing balances by summing transactions.

## Current limitations

- Version 1 analytics supports one shared account currency and currently validates
  GBP.
- Commit 18 categorisation is deterministic only; evaluated ML categorisation and
  the persistent correction/personal-rule workflow arrive in Commits 19 and 20.
- Recurrence detection and confirmation arrive in Commit 21.
- Budget planning now consumes these coverage indicators and verified breakdowns;
  APIs and visual dashboards remain later stages.
- Approved PDF rows are still review-only until their complete atomic persistence
  workflow is implemented.

## Confirmed recurring expenses

Confirmed candidate membership moves expense-role transactions from `unclassified`
to the `recurring` cadence bucket. Pending and cancelled candidates have no effect.
Discretionary spending is not inferred as merely the inverse of recurring, and all
headline cash-flow formulas remain unchanged.

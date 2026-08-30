# Recurring-payment detection

Detection uses owned, verified transactions and role-audit evidence available by an
explicit timezone-aware `knowledge_cutoff_at`. It never treats a transaction's
current role as historical truth. CSV candidates require confirmed raw lineage;
PDF/OCR candidates additionally require a fully verified document. Merchant text is
normalised for stable grouping without modifying stored descriptions. Raw rows must
also exist by the cutoff, and their import batch must name the verified transaction's
account. The policy explicitly supplies occurrence, amount, interval, and confidence
thresholds.

A candidate's full identity is account, normalised merchant, currency, direction,
financial role, and frequency. This prevents, for example, an incoming refund and an
outgoing expense for the same merchant from becoming one series. Candidates retain
`evidence_as_of_date` and `knowledge_cutoff_at`; each membership link retains the
`identified_at` time when that transaction became recurrence evidence.

Supported frequencies are weekly, fortnightly, monthly, quarterly, and annual.
Calendar advancement handles monthly, quarterly, annual, and end-of-month dates.

Expected dates are checked only against statement coverage imported and confirmed by
the cutoff. The timezone-aware cutoff must reach the start of the following UTC day,
so an in-progress `as_of_date` is never treated as complete. Complete and overlapping
coverage are known; explicit gaps are subtracted; partial and unknown statements prove
nothing. Only a known date can count as missed, count against the skipped-occurrence
limit, and reduce confidence. This applies both between observed occurrences and from
the latest occurrence through `as_of_date`. Explicit gaps never imply cancellation or
consume that limit.

Candidates begin pending. Confirmation creates an active recurring series;
cancellation is explicit and prevents silent recreation. Confirmation and cancellation
persist one server-generated UTC receipt time on the candidate and, for confirmation,
the linked series. The caller's aware review timestamp remains request metadata: a
future value is rejected and a backdated value cannot make a decision visible to an
earlier historical cutoff. The receipt must follow the candidate's detection,
knowledge cutoff, and membership evidence or the operation rolls back.

A detection request for an older cutoff reconstructs an as-of projection. If the
stored candidate has newer evidence or a later review, that historical projection is
returned without rewriting the newer persisted candidate or its review state. This is
not a general series-state history system: later cancellation/deactivation history
will require an explicit lifecycle design. This commit does not forecast balances,
infer anomalies, or add an API/UI.

Rows upgraded from the earlier `0005` schema have a stricter boundary. That schema
did not retain when a candidate's derived identity or each membership link became
available, so `0006` assigns both fields one migration-execution timestamp. Migrated
evidence is therefore unavailable to a historical forecast before that timestamp,
even when its original transaction, detection, or review date is older. This is an
intentional fail-closed quarantine rather than an invented historical timestamp; all
original recurrence and transaction evidence remains preserved for audit.
Before that marker, a migrated stored confirmation or cancellation is projected as
pending and cannot suppress or reclassify historical evidence. From the marker
onward, the marker is also the earliest effective availability time for the preserved
review and its linked members, so a confirmed legacy series can be used without
pretending its older caller-supplied review time was authoritative. Equal legacy
detection times are resolved deterministically by candidate ID; duplicate identities
remain preserved rather than silently merged.

## Manual verification

Run the complete fictional detect-confirm-refresh lifecycle:

```bash
make demo-recurrence
```

Expected output is:

```text
CashFlow AI synthetic recurrence check
merchant group: synthetic utility
frequency: monthly
first predicted date: 2025-04-30
review status: confirmed
occurrences after refresh: 4
refreshed predicted date: 2025-05-31
covered misses: 0
```

You can safely vary the fictional outgoing amount, for example:

```bash
uv run python scripts/demo_recurrence.py --amount=-31.50
```

`--amount` must be negative because the demo models an outgoing expense. Changing it
does not change the monthly dates or the expected lifecycle output above. The demo
uses an in-memory SQLite database and does not read or retain a real statement.

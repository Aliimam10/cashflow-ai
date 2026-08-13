# Recurring-payment detection

Detection uses verified, owned transactions with resolved financial roles. Merchant
text is normalised for stable grouping without modifying stored descriptions. The
policy explicitly supplies occurrence, amount, interval, and confidence thresholds.

Supported frequencies are weekly, fortnightly, monthly, quarterly, and annual.
Calendar advancement handles monthly, quarterly, annual, and end-of-month dates.

Expected dates are checked against verified coverage. Complete and overlapping
coverage are known; explicit gaps are subtracted; partial and unknown statements
prove nothing. Only a known date can count as missed and reduce confidence. A gap
never implies cancellation.

Candidates begin pending. Confirmation creates an active recurring series;
cancellation is explicit and prevents silent recreation. This commit does not
forecast balances, infer anomalies, or add an API/UI.

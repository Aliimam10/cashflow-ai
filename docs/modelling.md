# Modelling

CashFlow AI will use simple, explainable baselines before considering more
advanced models. scikit-learn is the primary planned ML library; statsmodels
will support statistical comparisons.

Forecasting will model confirmed recurring flows separately from discretionary
cash flow. All training and backtesting must respect historical cutoffs and
exclude future information.

## Synthetic data

`cashflow_ai.demo_data` produces deterministic one-to-three-year histories for
student, salaried-worker, and irregular-income profiles. Each history includes
recurring flows, discretionary purchases, price drift, running balances,
labelled unusual transactions, and exact and probable duplicate examples.

The generator uses only generic fictional merchants and configurable random
seeds. It is test data and demonstration data, not evidence of model quality.
Later evaluation must keep chronological cutoffs and compare models with the
required baselines even when using these labels.

# Modelling

CashFlow AI will use simple, explainable baselines before considering more
advanced models. scikit-learn is the primary planned ML library; statsmodels
will support statistical comparisons.

Forecasting will model confirmed recurring flows separately from discretionary
cash flow. All training and backtesting must respect historical cutoffs and
exclude future information.


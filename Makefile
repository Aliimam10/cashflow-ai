.PHONY: setup test check-import

setup:
	uv sync --dev

test:
	uv run pytest

check-import:
	uv run python -c "import cashflow_ai; print(cashflow_ai.__version__)"


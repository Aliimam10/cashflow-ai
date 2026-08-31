.PHONY: setup format format-check lint typecheck test coverage pre-commit check check-import check-ocr demo-data demo-recurrence demo-forecast demo-forecast-model demo-forecast-path demo-anomalies demo-model-registry demo-planning db-upgrade db-downgrade

setup:
	uv sync --dev

format:
	uv run ruff format .
	uv run ruff check --fix .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

coverage: test

pre-commit:
	uv run pre-commit run --all-files

check: format-check lint typecheck test

check-import:
	uv run python -c "import cashflow_ai; print(cashflow_ai.__version__)"

check-ocr:
	uv run python -c "from cashflow_ai.imports import PytesseractOcrEngine; PytesseractOcrEngine().ensure_available(); print('Local Tesseract OCR is available')"

demo-data:
	uv run python scripts/generate_demo_data.py --profile all

demo-recurrence:
	uv run python scripts/demo_recurrence.py

demo-forecast:
	uv run cashflow-forecast-demo --weeks 20 --test-weeks 3

demo-forecast-model:
	uv run cashflow-forecast-model-demo --weeks 36 --test-weeks 4

demo-forecast-path:
	uv run cashflow-forecast-path-demo --horizon-days 30 --simulations 200

demo-anomalies:
	uv run cashflow-anomaly-demo --history-transactions 30

demo-model-registry:
	uv run cashflow-model-registry-demo --activate synthetic-2

demo-planning:
	uv run cashflow-planning-demo

db-upgrade:
	uv run alembic upgrade head

db-downgrade:
	uv run alembic downgrade base

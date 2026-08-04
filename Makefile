.PHONY: setup format format-check lint typecheck test coverage pre-commit check check-import demo-data db-upgrade db-downgrade

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

demo-data:
	uv run python scripts/generate_demo_data.py --profile all

db-upgrade:
	uv run alembic upgrade head

db-downgrade:
	uv run alembic downgrade base

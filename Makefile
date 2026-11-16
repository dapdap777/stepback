.PHONY: check lint test fmt

# Everything CI runs, in the same order.
check: lint test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

test:
	uv run pytest -q

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

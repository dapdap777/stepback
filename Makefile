.PHONY: check lint test fmt

# Everything CI runs, in the same order.
check: lint test test-oldest

lint:
	uv run ruff check src tests examples
	uv run ruff format --check src tests examples
	uv run mypy

test:
	uv run pytest -q

fmt:
	uv run ruff check --fix src tests examples
	uv run ruff format src tests examples

# Same as CI's "anthropic oldest" jobs: a separate env pinned to the oldest supported SDK.
OLDEST_ENV ?= .venv-oldest
ANTHROPIC_OLDEST ?= 0.40.0
.PHONY: test-oldest
test-oldest:
	UV_PROJECT_ENVIRONMENT=$(OLDEST_ENV) uv sync --locked -q
	uv pip install -q --python $(OLDEST_ENV)/bin/python "anthropic==$(ANTHROPIC_OLDEST)"
	$(OLDEST_ENV)/bin/python -c "import anthropic, sys; v = anthropic.__version__; print('anthropic', v); sys.exit(v != '$(ANTHROPIC_OLDEST)')"
	UV_PROJECT_ENVIRONMENT=$(OLDEST_ENV) uv run --no-sync pytest -q

format-check:
	uv run ruff format --check

format:
	uv run ruff format

lint:
	uv run ruff check

lint-fix:
	uv run ruff check --fix

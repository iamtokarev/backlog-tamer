unhide-venv:
	chflags -R nohidden .venv

format-check:
	uv run ruff format --check

run:
	uv run python -m backlog_tamer.integrations.telegram.bot

format:
	uv run ruff format

lint:
	uv run ruff check

lint-fix:
	uv run ruff check --fix

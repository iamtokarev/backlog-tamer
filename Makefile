HOST ?= 127.0.0.1
PORT ?= 8000
WEBHOOK_PATH ?= /telegram/webhook
PUBLIC_URL ?=

ifneq ($(strip $(PUBLIC_URL)),)
WEBHOOK_PUBLIC_URL_ARG := --public-url $(PUBLIC_URL)
endif

unhide-venv:
	chflags -R nohidden .venv

format-check:
	uv run ruff format --check

run:
	uv run python -m backlog_tamer.integrations.telegram.bot

webhook-dev:
	uv run python -m backlog_tamer.integrations.telegram.webhook_dev --host $(HOST) --port $(PORT) --path $(WEBHOOK_PATH) $(WEBHOOK_PUBLIC_URL_ARG)

webhook-info:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ -z "$$TELEGRAM__BOT_TOKEN" ]; then \
		echo "TELEGRAM__BOT_TOKEN is not set"; exit 1; \
	fi; \
	curl -sS "https://api.telegram.org/bot$$TELEGRAM__BOT_TOKEN/getWebhookInfo"

webhook-clear:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ -z "$$TELEGRAM__BOT_TOKEN" ]; then \
		echo "TELEGRAM__BOT_TOKEN is not set"; exit 1; \
	fi; \
	curl -sS "https://api.telegram.org/bot$$TELEGRAM__BOT_TOKEN/deleteWebhook"

format:
	uv run ruff format

lint:
	uv run ruff check

lint-fix:
	uv run ruff check --fix

test:
	uv run pytest -v

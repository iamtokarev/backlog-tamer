## What this is

Backlog Tamer is a Telegram bot that turns messy learning/project inputs (links, notes) into structured Notion backlog items. An agent drafts a project/task proposal, a human approves or revises it via Telegram, and the approved result is written to Notion.

## Commands

Uses `uv` for dependency management (Python 3.12+, package lives in `src/backlog_tamer`).

```sh
make run            # Run the Telegram bot with local polling
make webhook-dev PUBLIC_URL=https://your-ngrok-url  # Local webhook server
make test           # uv run pytest -v
make lint           # uv run ruff check
make lint-fix       # uv run ruff check --fix
make format         # uv run ruff format
make format-check   # uv run ruff format --check (CI enforces this)
make webhook-info   # Inspect current Telegram webhook
make webhook-clear  # Delete Telegram webhook
```

Run a single test: `uv run pytest tests/test_telegram_webhook.py -v` (or `-k <pattern>`). `pythonpath = ["src"]` is set in pyproject, so tests import `backlog_tamer` directly.

CI runs `ruff format --check`, `ruff check`, and `pytest` with `--locked` — keep `uv.lock` in sync when changing dependencies. Deploy to AWS runs automatically from GitHub Actions after CI passes on `main`.

Requires a `.env` file (see `.env.example`). Settings use pydantic-settings with `__` as the nested delimiter (e.g. `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token`); see `src/backlog_tamer/config.py`.

## Architecture

Three layers under `src/backlog_tamer/`:

- **`agents/intake_triage/`** — Google ADK agent + workflow. `agent.py` defines the drafting agent (LiteLlm wrapping an OpenAI model, `output_schema=ProjectDraft`, writes to session state key `draft_proposal`). `workflow.py` builds an ADK `Workflow` graph: draft → `request_human_review` (emits a `RequestInput` interrupt) → `handle_human_review` routes to `approved` / `rejected` / `revise` (revise loops back to the draft agent with feedback). Human-in-the-loop is implemented via ADK interrupts, not chat turns.

- **`application/`** — orchestration, independent of Telegram. `IntakeService` (`intake_service.py`) runs the workflow with ADK's `Runner` + `DatabaseSessionService`, extracts the `adk_request_input` interrupt from events, and persists a `ConfirmationRecord` via `ConfirmationStore` (SQLAlchemy, `confirmations` table). `start_intake` creates a session and returns a `needs_review` result; `resume_intake` replays the review reply into the paused workflow; `finalize_approval` uses `mark_committing_once` as an idempotency lock before writing to Notion (statuses: PENDING_REVIEW → COMMITTING → COMMITTED / REJECTED / FAILED). `database_urls.py` converts one `DATABASE_URL` into the sync driver (psycopg/sqlite) for the store and the async driver (asyncpg/aiosqlite) for ADK sessions — support both SQLite (local) and Postgres/Supabase (deployed) when touching persistence.

- **`integrations/`** — `telegram/` has three entry points sharing the same handlers: `bot.py` (local polling), `webhook_dev.py` (local webhook server), and `lambda_handlers.py` (deployed: `webhook_handler` validates the secret/allowed user and enqueues raw updates to SQS; `worker_handler` consumes SQS records and runs the real processing, loading secrets from AWS Secrets Manager). `notion/writer.py` creates the project + task pages. Review actions arrive as inline-keyboard callbacks (approve/reject/revise) handled in `handlers.py`.

`dev/run_intake_workflow.py` runs the agent workflow standalone (in-memory sessions) without Telegram — useful for iterating on prompts/schema.

LangSmith tracing is configured lazily in `IntakeService._get_root_agent` when `LANGSMITH_API_KEY` is set.

## Infrastructure

Terraform in `infra/terraform/` (Lambda ×2, SQS + DLQ, ECR, Secrets Manager). `scripts/build_and_push_image.sh` builds the Lambda container image. Both Lambdas run from the same image; the handler name selects webhook vs worker.

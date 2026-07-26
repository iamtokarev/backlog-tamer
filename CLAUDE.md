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

CI runs `ruff format --check`, `ruff check`, and `pytest` with `--locked` — keep `uv.lock` in sync when changing dependencies.

Requires a `.env` file (see `.env.example`). Settings use pydantic-settings with `__` as the nested delimiter (e.g. `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token`); see `src/backlog_tamer/config.py`.

## Releases and deploys

Merging to `main` does **not** deploy. Production ships from releases:

1. Push conventional commits (`feat:`, `fix:`, …) to `main`. `release.yml` keeps a standing release PR open that bumps `pyproject.toml`, relocks `uv.lock`, and writes `CHANGELOG.md`.
2. Merge the release PR. release-please tags `vX.Y.Z` and cuts a GitHub release, CI re-runs as a gate, then `deploy.yml` builds the arm64 image, pushes it to ECR tagged `vX.Y.Z`, applies Terraform, and smoke-tests both Lambdas.

The smoke test invokes the worker with `{"healthcheck": true}` and asserts the reported version matches the tag, so a deploy that silently didn't take fails the job. `_healthcheck` in `lambda_handlers.py` eagerly imports the agent/Notion modules and calls `fetch_url.missing_optional_dependencies()` — the HTML and PDF extraction paths fall back silently when a dependency is missing from the image, so that probe is the only thing that surfaces it.

Roll back by dispatching the Deploy workflow with a previous tag:

```sh
gh workflow run deploy.yml -f version=v0.1.1
```

The image is already in ECR, so the build is skipped and only `terraform apply` runs. The ECR lifecycle policy keeps the last 10 `v`-tagged images as rollback targets. Version bumps are release-please's job — don't edit `version` in `pyproject.toml` by hand.

`bootstrap-sha` in `release-please-config.json` is the history boundary for the first release (the repo had no `v*` tags). It can be dropped once `v0.2.0` exists.

## Notion database schema

Property names live in `integrations/notion/writer.py` as module constants —
rename a column in Notion and it is a one-line change there.

Required (the deploy healthcheck fails without them):

- **Projects**: `Project name` (title), `Status`, `Priority`, `Tags` (multi-select), `Summary` (rich text)
- **Tasks**: `Task name` (title), `Status`, `Priority`, `Projects` (relation)

Optional, listed in `OPTIONAL_PROJECT_PROPERTIES` / `OPTIONAL_TASK_PROPERTIES`.
The writer reads each database's schema once and silently drops properties that
do not exist, so these can be added whenever — until then the feature is just
absent:

- **Projects**: `Source` (url), `Type` (select), `Intent` (select), `Captured` (date)
- **Tasks**: `Due` (date), `Source` (url)

Duplicate detection queries Projects by `Source`, so without that column every
send creates a new project. `worker_handler` with `{"healthcheck": true}`
reports which optional properties are being skipped.

## Architecture

Three layers under `src/backlog_tamer/`:

- **`agents/intake_triage/`** — Google ADK agent + workflow. `agent.py` defines the drafting agent (LiteLlm wrapping an OpenAI model, `output_schema=ProjectDraft`, writes to session state key `draft_proposal`). `workflow.py` builds an ADK `Workflow` graph: draft → `request_human_review` (emits a `RequestInput` interrupt) → `handle_human_review` routes to `approved` / `rejected` / `revise` (revise loops back to the draft agent with feedback). Human-in-the-loop is implemented via ADK interrupts, not chat turns.

- **`application/`** — orchestration, independent of Telegram. `IntakeService` (`intake_service.py`) runs the workflow with ADK's `Runner` + `DatabaseSessionService`, extracts the `adk_request_input` interrupt from events, and persists a `ConfirmationRecord` via `ConfirmationStore` (SQLAlchemy, `confirmations` table). `start_intake` creates a session and returns a `needs_review` result; `resume_intake` replays the review reply into the paused workflow; `finalize_approval` uses `mark_committing_once` as an idempotency lock before writing to Notion (statuses: PENDING_REVIEW → COMMITTING → COMMITTED / REJECTED / FAILED). `database_urls.py` converts one `DATABASE_URL` into the sync driver (psycopg/sqlite) for the store and the async driver (asyncpg/aiosqlite) for ADK sessions — support both SQLite (local) and Postgres/Supabase (deployed) when touching persistence.

- **`integrations/`** — `telegram/` has three entry points sharing the same handlers: `bot.py` (local polling), `webhook_dev.py` (local webhook server), and `lambda_handlers.py` (deployed: `webhook_handler` validates the secret/allowed user and enqueues raw updates to SQS; `worker_handler` consumes SQS records and runs the real processing, loading secrets from AWS Secrets Manager). `notion/writer.py` creates the project + task pages. Review actions arrive as inline-keyboard callbacks (approve/reject/revise) handled in `handlers.py`.

`dev/run_intake_workflow.py` runs the agent workflow standalone (in-memory sessions) without Telegram — useful for iterating on prompts/schema.

LangSmith tracing is configured lazily in `IntakeService._get_root_agent` when `LANGSMITH_API_KEY` is set.

## Infrastructure

Terraform in `infra/terraform/` (Lambda ×2, SQS + DLQ, ECR, Secrets Manager). `scripts/build_and_push_image.sh` builds the Lambda container image. Both Lambdas run from the same image; the handler name selects webhook vs worker.

<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

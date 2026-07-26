---
type: Integration
title: Telegram and Notion Integrations
description: >-
  How Backlog Tamer integrates with Telegram (three entry points, shared
  handlers, webhook validation, state management) and Notion (project and task
  page creation via the Notion API).
tags: [integrations, telegram, notion, webhook]
timestamp: 2025-01-20T00:00:00Z
---

# Telegram and Notion Integrations

## Telegram

### Three Entry Points

All three entry points build the same `python-telegram-bot` `Application` via `build_application` in `src/backlog_tamer/integrations/telegram/bot.py` and use the same handlers (`handle_message`, `handle_callback` from `handlers.py`).

| Entry Point | File | Use Case | How Updates Arrive |
|-------------|------|----------|-------------------|
| Polling | `bot.py` (`main()`) | Local development | `application.run_polling()` |
| Local webhook | `webhook_dev.py` | Testing webhook locally (ngrok) | HTTP server → queue → `TelegramUpdateProcessor` |
| Lambda | `lambda_handlers.py` | Production | AWS Lambda Function URL → SQS → `TelegramUpdateProcessor` |

### Lambda Healthcheck

`worker_handler` dispatches to a `_healthcheck()` function when the event contains `{"healthcheck": true}`. This is invoked by the [deploy workflow's smoke test](../operations/deployment.md) to verify the deployed image can do real work before traffic reaches it:

1. Eagerly imports the agent (`agent`, `workflow`), Notion (`writer`), and fetch_url modules — these are otherwise only loaded on the first real message, so a broken image would surface as a failed user message instead of a failed deploy.
2. Calls `fetch_url.missing_optional_dependencies()` to check that `beautifulsoup4` and `pypdf` are installed. Both loaders swallow `ImportError` and fall back silently, so a missing dependency degrades output quality without ever raising. The healthcheck surfaces this.
3. Reads the installed package version via `importlib.metadata.version("backlog-tamer")` and returns `{"ok": true, "version": "..."}`. The deploy workflow asserts this matches the release tag.

### Handler Logic

`handlers.py` defines two handlers registered in `build_application`:

- **`handle_message`** — processes text/caption messages from the allowed user. If a revision is pending for this chat (checked via `TelegramStateStore`), routes the text as revision feedback. Otherwise, calls `IntakeService.start_intake` with an `IncomingContext` parsed from the message.
- **`handle_callback`** — processes inline-keyboard callbacks matching `^(approve|revise|reject):`. For `approve`/`reject`, calls `IntakeService.resume_intake` directly. For `revise`, stores the `confirmation_id` in `TelegramStateStore` and prompts the user to send feedback text.

### Message Parsing

`parsing.py` extracts an `IncomingContext` from a Telegram `Message`:

1. `raw_text` = message text or caption.
2. URLs are extracted from `MessageEntity.URL` entities (visible links) and `MessageEntity.TEXT_LINK` entities (hidden text links).
3. `note` = raw_text with visible URLs stripped and whitespace collapsed.
4. Links are deduplicated preserving order.

### Webhook Validation

`webhook.py` provides `validate_webhook_update`, which checks:

1. **Secret token** — `x-telegram-bot-api-secret-token` header compared via `hmac.compare_digest` (if configured).
2. **Update ID** — must be an integer.
3. **User authorization** — sender `id` must match `allowed_user_id`.
4. **Supported update type** — must contain `message` or `callback_query` dict.

Unsupported or unauthorized updates return 200 "ignored" (not an error). Invalid secret returns 403 "forbidden".

### Telegram State

`state.py` defines `TelegramStateStore` with two tables:

- **`telegram_revision_states`** — tracks which `confirmation_id` a user is currently revising per (user_id, chat_id). `set_awaiting_revision` stores it; `pop_awaiting_revision` reads and deletes it (one-shot).
- **`telegram_processed_updates`** — deduplication for Lambda worker. `record_update_once` returns `True` if this is the first time seeing this `update_id`, `False` if already processed. Prevents duplicate processing from SQS retries.

### Rendering

`rendering.py` builds:

- `render_draft_message(draft)` — Markdown V2 formatted message with project name, type, intent, priority, source URL, summary, and tasks.
- `build_review_keyboard(confirmation_id)` — inline keyboard with three buttons: Approve (`approve:{id}`), Revise (`revise:{id}`), Reject (`reject:{id}`).
- `render_terminal_message(draft, status, notion_url)` — final message after commit/reject/fail with a status badge and optional Notion link.

## Notion

### NotionWriter

`src/backlog_tamer/integrations/notion/writer.py` — creates project and task pages in Notion databases via the Notion REST API (`https://api.notion.com/v1/pages`).

**`create_project_with_tasks(draft)`** is the main method:

1. Builds a project page payload and POSTs it to the Notion API.
2. Extracts `project_id` and `project_url` from the response.
3. For each task in `draft.tasks`, builds a task payload with a `Projects` relation to the project page and POSTs it.
4. Returns `NotionCommitResult(project_id, project_url, task_ids)`.

**Key design choices:**

- Uses `template: {type: "default"}` on both project and task pages to apply Notion's default templates (added in commit `93f0798`).
- Project status is hardcoded to `"Backlog"`; task status to `"Not started"`.
- Tags are derived from `resource_type` and `intent`, with `unknown` resource type omitted and `unclear` intent mapped to `explore`.
- Summary field includes the source URL, resource type, and intent appended to the draft summary.
- `NotionWriter.from_settings(settings)` is a factory that reads credentials from the `Settings` model.
- Uses `httpx.AsyncClient` with a 20-second timeout. Accepts an optional injected client for testing.

### Notion Configuration

Required env vars (see `.env.example`):

| Variable | Maps To |
|----------|---------|
| `NOTION_TOKEN` | `settings.notion_token` |
| `NOTION_PROJECTS_DATABASE_ID` | `settings.notion_projects_database_id` |
| `NOTION_TASKS_DATABASE_ID` | `settings.notion_tasks_database_id` |
| `NOTION_API_VERSION` | `settings.notion_api_version` (default: `2022-06-28`) |

The Notion database must have properties named: `Project name`, `Status`, `Priority`, `Tags`, `Summary` (projects database) and `Task name`, `Status`, `Priority`, `Projects` (tasks database).

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/integrations/telegram/bot.py` | `build_application`, polling entry point |
| `src/backlog_tamer/integrations/telegram/handlers.py` | `handle_message`, `handle_callback` |
| `src/backlog_tamer/integrations/telegram/parsing.py` | `build_incoming_context` |
| `src/backlog_tamer/integrations/telegram/webhook.py` | `TelegramUpdateProcessor`, `validate_webhook_update` |
| `src/backlog_tamer/integrations/telegram/webhook_dev.py` | Local webhook server |
| `src/backlog_tamer/integrations/telegram/lambda_handlers.py` | Lambda `webhook_handler` and `worker_handler` |
| `src/backlog_tamer/integrations/telegram/rendering.py` | Markdown rendering, inline keyboards |
| `src/backlog_tamer/integrations/telegram/state.py` | `TelegramStateStore` (revision tracking, update dedup) |
| `src/backlog_tamer/integrations/notion/writer.py` | `NotionWriter` |
| `src/backlog_tamer/agents/intake_triage/tools/fetch_url.py` | `missing_optional_dependencies()` used by Lambda healthcheck |

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

- **`handle_message`** — processes text/caption messages from the allowed user. If a revision is pending for this chat (checked via `TelegramStateStore`), routes the text as revision feedback. Otherwise, calls `IntakeService.start_intake` with an `IncomingContext` parsed from the message. Shows a progress message while the agent works.
- **`handle_callback`** — processes inline-keyboard callbacks. Beyond the original `approve`/`revise`/`reject`, it now handles:
  - **Quick edits** (`edit`, `pick`, `back`) — opens a field picker, applies a one-tap change to priority/intent/type without re-running the agent, or returns to the review keyboard. Calls `ConfirmationStore.apply_manual_edit` directly.
  - **Refetch** (`refetch`) — clears the fetch_url cache and re-runs the agent with a "fetch again" instruction, for when the original page fetch failed.
  - **Retry** (`retry`) — re-attempts the Notion write after a FAILED status.
  - **Undo** (`undo`) — archives the Notion pages created by a committed confirmation via `IntakeService.undo_commit`.
  - **Add task** (`addtask`) — attaches this draft's tasks to an existing duplicate project via `IntakeService.add_to_existing_project`.
  - **Cancel** (`cancel`) — exits revision mode and restores the review keyboard.

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

`state.py` defines `TelegramStateStore` with two tables. The store uses `NullPool` when the database URL goes through an external pooler (e.g. Supabase's transaction-mode pooler on port 6543), via `uses_external_pooler()` from `database_urls.py`.

- **`telegram_revision_states`** — tracks which `confirmation_id` a user is currently revising per (user_id, chat_id). `set_awaiting_revision` stores it; `pop_awaiting_revision` reads and deletes it (one-shot). Also accessible via helper functions `get_session_revision` and `set_session_revision` which use the store if present, falling back to `context.user_data`.
- **`telegram_processed_updates`** — deduplication for Lambda worker. `record_update_once` returns `True` if this is the first time seeing this `update_id`, `False` if already processed. Prevents duplicate processing from SQS retries.

### Rendering

`rendering.py` renders all messages as **HTML** (not MarkdownV2) using `telegram.constants.ParseMode.HTML`. It builds:

- `render_progress_message(incoming)` — shown while the agent works (e.g. "🔎 Reading example.com…" or "🧠 Triaging your note…"), replacing the old typing indicator.
- `render_draft_message(draft, grounding)` — HTML review card: bold title, a chip line (resource type · intent · priority with emoji icons), summary, source link with site name, task checklist, topic tags, and a fetch-warning footer when grounding is degraded.
- `build_review_keyboard(draft, confirmation_id, grounding)` — inline keyboard with: Approve & save / Reject (row 1), quick-edit pickers for priority, intent, and type (row 2), Revise with a note (row 3), and a conditional "Retry fetch" button when grounding is degraded.
- `build_picker_keyboard(field_code, confirmation_id)` — replaces the review keyboard with the options for one enum field (priority/intent/type), plus a Back button.
- `render_change_summary(before, after)` — one-line summary of what a revision moved (priority, intent, type, task count, title, summary, source).
- `render_terminal_message(draft, status, notion_url, failure_reason, duplicate_created_time)` — final message after commit/reject/fail/undo/duplicate with a status badge. For FAILED, the full draft is re-shown with a retry button. For DUPLICATE, shows when the existing project was created. For UNDONE, tells the user to re-send if they want it back.
- `build_terminal_keyboard(status, confirmation_id, notion_url)` — post-resolution keyboard: FAILED shows "Retry save"; COMMITTED shows "Open in Notion" + "Undo"; DUPLICATE shows "Open existing" + "Add task there".

## Notion

### NotionWriter

`src/backlog_tamer/integrations/notion/writer.py` — creates project and task pages in Notion databases via the Notion REST API (`https://api.notion.com/v1/pages`).

**`create_project_with_tasks(draft, incoming_context, grounding)`** is the main method:

1. Fits the project payload to the database schema (drops unknown properties).
2. Builds a project page payload (with icon, properties, and a page body of children blocks) and POSTs it.
3. Extracts `project_id` and `project_url` from the response.
4. For each task in `draft.tasks`, builds a task payload with a `Projects` relation, due date, and optional source URL, fits it to schema, and POSTs all tasks concurrently via `asyncio.gather`.
5. Returns `NotionCommitResult(project_id, project_url, task_ids)`.

**Additional methods:**

- `find_project_by_source(source_url)` — queries the projects database for an existing page with the same Source URL, for duplicate detection. Never blocks the commit on failure.
- `add_tasks_to_project(project_id, draft)` — attaches tasks to an existing project (used when a duplicate is found and the user chooses "Add task there").
- `archive_pages(page_ids)` — archives (not deletes) project and task pages, used by the undo flow.
- `describe_schema()` — compares the writer's expected properties against what the databases actually have, returning a `NotionSchemaReport` with missing and skipped properties. Called from the healthcheck.

**Key design choices:**

- One `httpx.AsyncClient` per commit session (shared across all page POSTs in a commit) via the `_session()` context manager. 20-second timeout. Accepts an optional injected client for testing.
- Project pages do **not** send `template: {type: "default"}` — Notion rejects a page that sends both a template and children blocks. Task pages still use the default template.
- Project page body (`build_project_children`) includes: a bookmark block for the source URL, a "Why I saved this" heading with the user's note, "Key points" from grounding, "Next action" to-do blocks for tasks, and a provenance callout.
- Icon is set from a `resource_type` → emoji mapping.
- Tags come from `draft.topics` (not type/intent restatement).
- Task due dates: High = +3 days, Medium = +14 days, Low = no due date.
- `_fit_to_schema` probes each database's properties and drops any optional properties the database does not have, so a workspace that has not added new columns still gets a usable page.
- Optional project properties: Source, Type, Intent, Captured. Optional task properties: Due, Source.

### Notion Configuration

Required env vars (see `.env.example`):

| Variable | Maps To |
|----------|---------|
| `NOTION_TOKEN` | `settings.notion_token` |
| `NOTION_PROJECTS_DATABASE_ID` | `settings.notion_projects_database_id` |
| `NOTION_TASKS_DATABASE_ID` | `settings.notion_tasks_database_id` |
| `NOTION_API_VERSION` | `settings.notion_api_version` (default: `2022-06-28`) |

The Notion projects database must have properties named: `Project name`, `Status`, `Priority`, `Tags`, `Summary`. Optional (silently skipped if missing): `Source`, `Type`, `Intent`, `Captured`. The tasks database must have: `Task name`, `Status`, `Priority`, `Projects`. Optional: `Due`, `Source`.

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/integrations/telegram/bot.py` | `build_application`, polling entry point |
| `src/backlog_tamer/integrations/telegram/handlers.py` | `handle_message`, `handle_callback` |
| `src/backlog_tamer/integrations/telegram/parsing.py` | `build_incoming_context` |
| `src/backlog_tamer/integrations/telegram/webhook.py` | `TelegramUpdateProcessor`, `validate_webhook_update` |
| `src/backlog_tamer/integrations/telegram/webhook_dev.py` | Local webhook server |
| `src/backlog_tamer/integrations/telegram/lambda_handlers.py` | Lambda `webhook_handler` and `worker_handler` |
| `src/backlog_tamer/integrations/telegram/rendering.py` | HTML rendering, inline keyboards, picker keyboards, terminal keyboards |
| `src/backlog_tamer/integrations/telegram/state.py` | `TelegramStateStore` (revision tracking, update dedup) |
| `src/backlog_tamer/integrations/notion/writer.py` | `NotionWriter` |
| `src/backlog_tamer/agents/intake_triage/tools/fetch_url.py` | `missing_optional_dependencies()` used by Lambda healthcheck |

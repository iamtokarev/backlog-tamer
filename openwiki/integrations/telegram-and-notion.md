---
type: Integration
title: Telegram and Notion Integrations
description: >-
  How Backlog Tamer integrates with Telegram (three entry points, shared
  handlers, webhook validation, state management) and Notion (project page
  creation via the Notion API; no task pages).
tags: [integrations, telegram, notion, webhook]
timestamp: 2026-11-06T00:00:00Z
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

1. Eagerly imports `source_reader` (otherwise only loaded on the first real message), then calls `Triage.from_settings(settings).probe()` — which calls the configured decision and title models once each, so a retired slug, a typo, or a bad `OPENROUTER_API_KEY` fails the deploy instead of the first capture.
2. Probes the Notion Projects database via `NotionWriter.describe_schema()` and raises if `is_healthy` is false — a property renamed in Notion otherwise surfaces as a 400 during `finalize_approval`, after the user approved. (`NotionSchemaReport.is_healthy` is false when `missing_project_properties` is non-empty.)
3. Reads the installed package version via `importlib.metadata.version("backlog-tamer")` and returns `{"ok": true, "version": "...", "models": {"decision": ..., "title": ...}, "skipped_notion_properties": [...], "degraded_capabilities": [...]}`. The deploy workflow asserts the version matches the release tag and that both models are reported. `degraded_capabilities` surfaces schema gaps that disable a feature — currently a missing `Source` column disables `"duplicate-detection"`.

### Handler Logic

`handlers.py` defines two handlers registered in `build_application`:

- **`handle_message`** — processes text/caption messages from the allowed user. If a revision is pending for this chat (checked via `TelegramStateStore`), routes the text as revision feedback. Otherwise, calls `IntakeService.start_intake` with an `IncomingContext` parsed from the message. Shows a progress message while the source is read and Triage classifies.
- **`handle_callback`** — processes inline-keyboard callbacks. Beyond `approve`/`revise`/`reject`, it handles:
  - **Quick edits** (`edit`, `pick`, `back`) — opens a field picker, applies a one-tap change to priority/intent/project type without calling Triage, or returns to the review keyboard. Calls `IntakeService.apply_quick_edit`.
  - **Refetch** (`refetch`) — re-reads the source and re-classifies via `IntakeService.refetch`, for when the original page fetch failed.
  - **Retry** (`retry`) — re-attempts the Notion write after a FAILED status.
  - **Undo** (`undo`) — archives the Notion page created by a committed confirmation via `IntakeService.undo_commit`.
  - **Cancel** (`cancel`) — exits revision mode and restores the review keyboard.

The `addtask` callback from the agent era is gone — drafts carry no tasks.

### Callback Wire Format

`callbacks.py` encodes and decodes the `callback_data` carried by inline buttons in one place. The wire format (`action:confirmation_id`, `edit:field:confirmation_id`, `pick:field:value:confirmation_id`) outlives a deploy, so cards already sent keep working. Single-letter field codes (`p`, `i`, `t`) keep `callback_data` inside Telegram's 64-byte limit once a 36-character confirmation id is appended. `CALLBACK_DATA_LIMIT = 64`.

### Message Parsing

`parsing.py` extracts an `IncomingContext` from a Telegram `Message`:

1. `raw_text` = message text or caption.
2. URLs are extracted from `MessageEntity.URL` entities (visible links) and `MessageEntity.TEXT_LINK` entities (hidden text links).
3. `note` = raw_text with visible URLs stripped and whitespace collapsed.
4. Links are deduplicated preserving order.

### Webhook Validation

`webhook.py` provides `validate_webhook_secret` (standalone) and `validate_webhook_update` (which delegates to it). The secret check is **fail-closed**: if `webhook_secret` is `None` or empty, `validate_webhook_secret` returns `accepted=False` with `reason="missing_secret"`. If the secret is configured but the `x-telegram-bot-api-secret-token` header is absent or does not match (`hmac.compare_digest`), it returns `reason="invalid_secret"`.

`validate_webhook_update` checks: secret token (fail-closed), update ID (must be integer), user authorization (sender `id` matches `allowed_user_id`), and supported update type (must contain `message` or `callback_query`).

Unsupported or unauthorized updates return 200 "ignored". Invalid or missing secret returns 403 "forbidden".

#### Lambda webhook handler

`lambda_handlers.py` calls `validate_webhook_secret` **before** decoding the JSON body. If the secret is not configured (`missing_secret`), it returns **503** "service unavailable" — the function is intentionally unavailable without authentication. If the secret is configured but the header doesn't match (`invalid_secret`), it returns **403** "forbidden".

#### Local webhook server hardening

`webhook_dev.py` enforces additional protections: required `TELEGRAM__WEBHOOK_SECRET` (raises at startup if unset), body size limit (1 MB, 413/411/400), 10-second socket read timeout (408), and early authentication before reading the body.

### Telegram State

`state.py` defines `TelegramStateStore` with two tables. The store uses `NullPool` when the database URL goes through an external pooler (Supabase transaction-mode pooler), via `uses_external_pooler()` from `database_urls.py`.

- **`telegram_revision_states`** — tracks which `confirmation_id` a user is currently revising per (user_id, chat_id). `set_awaiting_revision` stores it; `pop_awaiting_revision` reads and deletes it (one-shot). Also accessible via helper functions `get_session_revision` and `set_session_revision` which use the store if present, falling back to `context.user_data`.
- **`telegram_processed_updates`** — deduplication for Lambda worker. `record_update_once` returns `True` if this is the first time seeing this `update_id`, `False` if already processed.

### Rendering

`rendering.py` renders all messages as **HTML** using `telegram.constants.ParseMode.HTML`. It builds:

- `render_progress_message(incoming)` — shown while the source is read and Triage classifies (e.g. "🔎 Reading example.com…" or "🧠 Triaging your note…").
- `render_draft_message(draft, grounding)` — HTML review card: bold title, a chip line (project type · intent · priority with emoji icons), summary, source link with site name, topic tags, and a fetch-warning footer when grounding is degraded. No task checklist — drafts carry no tasks.
- `build_review_keyboard(draft, confirmation_id, grounding)` — inline keyboard with: Approve & save / Reject (row 1), quick-edit pickers for priority, intent, and project type (row 2), Revise with a note (row 3), and a conditional "Retry fetch" button when grounding is degraded.
- `build_picker_keyboard(field, confirmation_id)` — replaces the review keyboard with the options for one enum field, plus a Back button.
- `render_change_summary(before, after)` — one-line summary of what a revision moved (priority, intent, project type, title, source).
- `render_terminal_message(draft, status, notion_url, failure_reason, duplicate_created_time)` — final message after commit/reject/fail/undo/duplicate with a status badge. For FAILED, the full draft is re-shown with a retry button.
- `build_terminal_keyboard(status, confirmation_id, notion_url)` — post-resolution keyboard: FAILED shows "Retry save"; COMMITTED shows "Open in Notion" + "Undo"; DUPLICATE shows "Open existing".

## Notion

### NotionWriter

`src/backlog_tamer/integrations/notion/writer.py` — creates a single project page in the Notion Projects database via the Notion REST API (`https://api.notion.com/v1/pages`). There are no task pages.

**`create_project(draft, incoming_context, grounding)`** is the main method:

1. Fits the project payload to the database schema (`_fit_to_schema` drops unknown properties).
2. Builds a project page payload (icon, properties, page-body children) and POSTs it.
3. Returns `NotionCommitResult(project_id, project_url)`.

**Additional methods:**

- `find_project_by_source(source_url)` — queries the Projects database for an existing page with the same Source URL, for duplicate detection. Never blocks the commit on failure.
- `list_tag_options()` — reads the Tags column's `multi_select` options and returns the existing tag spellings. Fed into `Triage.classify` via `IntakeService._known_topics()` so the decision model reuses an existing tag instead of minting a near-duplicate. Best-effort: returns `[]` on any error.
- `archive_pages(page_ids)` — archives (not deletes) project pages, used by the undo flow.
- `describe_schema()` — compares expected properties against what the database has, returning a `NotionSchemaReport` with `missing_project_properties`, `skipped_project_properties`, and `degraded_capabilities`. Called from the healthcheck.

**Key design choices:**

- One `httpx.AsyncClient` per commit session via the `_session()` context manager. 20-second timeout. Accepts an optional injected client for testing.
- Project pages do **not** send `template: {type: "default"}` — Notion rejects a page that sends both a template and children.
- Page body (`build_project_children`): bookmark block, "Why I saved this" heading with the user's note, "Key points" from grounding, and a provenance callout.
- Icon is set from a `project_type` → emoji mapping.
- Tags come from `draft.topics`, folded onto existing spellings via `fold_tag`/`_tag_key`.
- `commit_source_url(draft, grounding)` prefers `grounding.canonical_url` for duplicate detection.
- `_fit_to_schema` probes each database's properties and drops optional properties the database does not have, so a workspace that has not added new columns still gets a usable page. When the database still has the legacy `"Type"` column, it renames `Project type` to `Type` on the fly.

### Notion Configuration

Required env vars (see `.env.example`):

| Variable | Maps To |
|----------|---------|
| `NOTION_TOKEN` | `settings.notion_token` |
| `NOTION_PROJECTS_DATABASE_ID` | `settings.notion_projects_database_id` |
| `NOTION_API_VERSION` | `settings.notion_api_version` (default: `2022-06-28`) |

The Projects database must have: `Project name`, `Status`, `Priority`, `Tags`, `Summary`. Optional (silently skipped if missing): `Source`, `Project type` (or legacy `Type`), `Intent`, `Captured`. There is no tasks database.

### Schema Migration Script

`scripts/migrate_notion_schema.py` is a one-time operational script that adds the optional columns the writer had been silently dropping (`Project type`, `Intent`, `Source`, `Captured`) and backfills them for existing pages. Run from the repo root:

```sh
uv run --locked python scripts/migrate_notion_schema.py            # dry run
uv run --locked python scripts/migrate_notion_schema.py --apply     # add columns + backfill
```

Dry-run is the default; `--apply` writes. Additive only (never edits Tags, Summary, Status, Priority, names, or relations). Snapshots every page before writing. See `docs/setup.md`.

## Source Reading

Source reading — turning a public URL into the `FetchedUrl` Grounding that Triage drafts from — is a separate integration backed by Browserbase and Stagehand. It is documented in [Source Reader](source-reader.md): the extraction escalation ladder (Fetch API → structured Fetch JSON → hosted Stagehand rendering), local SSRF validation, YouTube/X oEmbed and PDF specialization, the per-invocation SDK client lifecycle, and `FetchedUrl.to_grounding()`.

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/integrations/telegram/bot.py` | `build_application`, polling entry point |
| `src/backlog_tamer/integrations/telegram/handlers.py` | `handle_message`, `handle_callback` |
| `src/backlog_tamer/integrations/telegram/callbacks.py` | `encode_callback`, `decode_callback`, `Action` enum |
| `src/backlog_tamer/integrations/telegram/parsing.py` | `build_incoming_context` |
| `src/backlog_tamer/integrations/telegram/webhook.py` | `TelegramUpdateProcessor`, `validate_webhook_update`, `validate_webhook_secret` |
| `src/backlog_tamer/integrations/telegram/webhook_dev.py` | Local webhook server |
| `src/backlog_tamer/integrations/telegram/lambda_handlers.py` | Lambda `webhook_handler` and `worker_handler`, secret loading |
| `src/backlog_tamer/integrations/telegram/rendering.py` | HTML rendering, inline keyboards, picker/terminal keyboards |
| `src/backlog_tamer/integrations/telegram/state.py` | `TelegramStateStore` (revision tracking, update dedup) |
| `src/backlog_tamer/integrations/notion/writer.py` | `NotionWriter` (`create_project`) |
| `src/backlog_tamer/integrations/source_reader.py` | `SourceReader` (Browserbase + Stagehand) behind `IntakeService._read`; see [Source Reader](source-reader.md) |
| `src/backlog_tamer/source_urls.py` | `normalize_source_url`, `strip_tracking_params`, `youtube_watch_url` |
| `scripts/migrate_notion_schema.py` | One-time Notion schema migration |
---
type: Data Model
title: Data Model
description: >-
  Pydantic schemas, SQLAlchemy tables, and Notion payload structures used by
  Backlog Tamer, including ProjectDraft, IncomingContext, ConfirmationRecord,
  and the confirmations/telegram state tables.
tags: [data-model, schema, database]
timestamp: 2025-01-20T00:00:00Z
---

# Data Model

Backlog Tamer uses Pydantic models for agent I/O and internal data transfer, SQLAlchemy tables for durable state, and Notion API payloads for external writes.

## Agent Schemas

All defined in `src/backlog_tamer/agents/intake_triage/schemas.py`.

### IncomingContext

The parsed user input from a Telegram message. Built by `parsing.py` from Telegram `Message` entities.

| Field | Type | Notes |
|-------|------|-------|
| `raw_text` | `str` | Full message text or caption, min length 1 |
| `note` | `str \| None` | Text with URLs stripped out |
| `links` | `list[SourceLink]` | Deduplicated URLs from message entities and text links |

### ProjectDraft

The agent's structured output. Written to session state under key `draft_proposal` and persisted as JSON in the `confirmations` table.

| Field | Type | Allowed Values |
|-------|------|-----------------|
| `project_name` | `str` | 4–10 word descriptive phrase (never bare names) |
| `summary` | `str` | 1–600 chars |
| `resource_type` | `Literal` | `article`, `paper`, `video`, `course`, `documentation`, `repository`, `idea`, `unknown` |
| `intent` | `Literal` | `learn`, `build`, `research`, `explore`, `reference`, `unclear` |
| `priority` | `Literal` | `Low`, `Medium`, `High` |
| `source_url` | `str \| None` | Original URL if available |
| `tasks` | `list[str] \| None` | Default 1 task; up to 5 if user requests breakdown |

### FetchedUrl

Result of the `fetch_url` tool. Stored in session state under `fetched_context`.

| Field | Type | Notes |
|-------|------|-------|
| `status` | `Literal["success", "error"]` | |
| `requested_url` | `str` | Original URL passed to tool |
| `final_url` | `str \| None` | After redirects |
| `canonical_url` | `str \| None` | From `<link rel="canonical">` |
| `domain` | `str \| None` | |
| `page_kind` | `Literal` | `html`, `pdf`, `text`, `unknown` |
| `title`, `description`, `site_name`, `author`, `published_at` | `str \| None` | Extracted metadata |
| `key_points` | `list[str]` | Up to 5 extracted points |
| `content_preview` | `str \| None` | Max 1600 chars |
| `error` | `str \| None` | Error reason if status is error |

## Application Models

Defined in `src/backlog_tamer/application/models.py`.

### ConfirmationStatus

```python
class ConfirmationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    COMMITTING = "committing"
    COMMITTED = "committed"
    REJECTED = "rejected"
    FAILED = "failed"
```

### ConfirmationRecord

The full state of an intake item, persisted in the `confirmations` table.

| Field | Type | Notes |
|-------|------|-------|
| `confirmation_id` | `str` | UUID, primary key |
| `user_id` | `str` | Telegram user ID |
| `chat_id` | `str \| None` | Telegram chat ID |
| `source_message_id` | `str \| None` | Telegram message ID |
| `session_id` | `str` | ADK session ID |
| `invocation_id` | `str` | ADK invocation ID (for resume) |
| `request_input_call_id` | `str` | ADK function call ID (for resume) |
| `status` | `ConfirmationStatus` | Current lifecycle state |
| `incoming_context` | `IncomingContext` | Original user input |
| `draft_proposal` | `ProjectDraft` | Latest draft |
| `review_message` | `str` | Human-readable review prompt |
| `created_at`, `updated_at` | `datetime` | Timestamps |
| `resolved_at` | `datetime \| None` | Set when terminal |
| `notion_project_id` | `str \| None` | Notion page ID after commit |
| `notion_project_url` | `str \| None` | Notion page URL after commit |
| `failure_reason` | `str \| None` | Error details if FAILED |

### IntakeResult

The return type from `IntakeService` methods, consumed by Telegram handlers.

| Field | Type |
|-------|------|
| `status` | `str` |
| `confirmation_id` | `str \| None` |
| `draft_proposal` | `ProjectDraft \| None` |
| `review_message` | `str \| None` |
| `notion_project_url` | `str \| None` |

## Database Tables

### `confirmations` (ConfirmationStore)

Defined in `src/backlog_tamer/application/confirmation_store.py`. Uses SQLAlchemy ORM. JSON fields store `IncomingContext` and `ProjectDraft` as serialized strings.

### `telegram_revision_states` (TelegramStateStore)

Defined in `src/backlog_tamer/integrations/telegram/state.py`. Tracks which confirmation a user is currently revising per chat.

| Column | Type | Notes |
|--------|------|-------|
| `state_key` | `String(255)` | PK, composite of user_id + chat_id |
| `user_id` | `String(255)` | |
| `chat_id` | `String(255)` | |
| `confirmation_id` | `String(64)` | Which draft is being revised |
| `created_at`, `updated_at` | `DateTime` | |

### `telegram_processed_updates` (TelegramStateStore)

Deduplication table for Lambda worker. Prevents processing the same Telegram update twice if SQS delivers duplicates.

| Column | Type | Notes |
|--------|------|-------|
| `update_id` | `String(64)` | PK, Telegram update_id |
| `created_at` | `DateTime` | |

## Entity Relationships

```mermaid
erDiagram
    confirmations {
        string confirmation_id PK
        string user_id
        string chat_id
        string source_message_id
        string session_id
        string invocation_id
        string request_input_call_id
        string status
        text incoming_context_json
        text draft_proposal_json
        text review_message
        datetime created_at
        datetime updated_at
        datetime resolved_at
        string notion_project_id
        text notion_project_url
        text failure_reason
    }

    telegram_revision_states {
        string state_key PK
        string user_id
        string chat_id
        string confirmation_id
        datetime created_at
        datetime updated_at
    }

    telegram_processed_updates {
        string update_id PK
        datetime created_at
    }

    confirmations ||--o{ telegram_revision_states : "references via confirmation_id"
```

## Notion Payload Structure

`NotionWriter` (`src/backlog_tamer/integrations/notion/writer.py`) builds two types of Notion page payloads:

### Project Page

Created in the projects database with:
- **Project name** (title): `draft.project_name`
- **Status** (status): `"Backlog"` (fixed default)
- **Priority** (select): `draft.priority`
- **Tags** (multi_select): `[resource_type, intent]` (skips `unknown` resource_type, maps `unclear` intent to `explore`)
- **Summary** (rich_text): `summary` + source URL + type + intent appended
- Uses `template: {type: "default"}` to apply Notion default templates

### Task Page

Created in the tasks database for each entry in `draft.tasks`:
- **Task name** (title): task name string
- **Status** (status): `"Not started"` (fixed default)
- **Priority** (select): inherited from project draft priority
- **Projects** (relation): `[{id: project_id}]` linking back to the created project page
- Uses `template: {type: "default"}`

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/agents/intake_triage/schemas.py` | `IncomingContext`, `ProjectDraft`, `FetchedUrl`, `SourceLink` |
| `src/backlog_tamer/application/models.py` | `ConfirmationRecord`, `ConfirmationStatus`, `IntakeResult` |
| `src/backlog_tamer/application/confirmation_store.py` | `ConfirmationRow` ORM, `confirmations` table |
| `src/backlog_tamer/integrations/telegram/state.py` | `TelegramRevisionRow`, `TelegramUpdateRow` ORM |
| `src/backlog_tamer/integrations/notion/writer.py` | Notion page payload builders |

---
type: Architecture Overview
title: Architecture Overview
description: >-
  Three-layer architecture of Backlog Tamer: AI agent workflow (Google ADK),
  application orchestration (IntakeService + ConfirmationStore), and
  integrations (Telegram + Notion).
tags: [architecture, overview]
timestamp: 2025-01-20T00:00:00Z
---

# Architecture Overview

Backlog Tamer is structured in three layers under `src/backlog_tamer/`. Each layer has a clear responsibility and the layers connect through well-defined interfaces.

## Layers

### 1. Agent Layer — `agents/intake_triage/`

The AI triage layer. A Google ADK `Agent` wraps an OpenAI model via LiteLLM and produces a structured `ProjectDraft` as output. The agent is embedded in a `Workflow` graph that implements the human-in-the-loop approval cycle using ADK interrupts.

- **`agent.py`** — defines `draft_agent` (the LLM agent) and `root_agent` (the workflow wrapping the agent). The agent uses `output_schema=ProjectDraft` and `output_key="draft_proposal"` to write structured drafts into session state.
- **`workflow.py`** — builds the ADK `Workflow` graph: `draft → request_human_review → handle_human_review → {approved, rejected, revise}`. The `request_human_review` node emits a `RequestInput` interrupt that pauses execution until the user responds.
- **`prompts.py`** — system instructions, triage prompt templates, review message templates, and revision prompt builder.
- **`schemas.py`** — Pydantic models: `IncomingContext`, `ProjectDraft`, `FetchedUrl`, `SourceLink`, `ReviewDecision`.
- **`tools/fetch_url.py`** — a tool the agent can call to fetch and extract context from web URLs, with SSRF protection and support for HTML, PDF, and X/Twitter oEmbed.

The agent layer is detailed further in [Intake Workflow](../workflows/intake-flow.md).

### 2. Application Layer — `application/`

Orchestration independent of Telegram. `IntakeService` runs the ADK workflow, manages sessions, extracts interrupts from events, and persists confirmation state. It is the bridge between the agent layer and the integrations.

- **`intake_service.py`** — `IntakeService` class with three main methods: `start_intake`, `resume_intake`, `finalize_approval`. Uses ADK `Runner` + `DatabaseSessionService` for workflow execution. Extracts `adk_request_input` interrupts from event streams to detect when human review is needed.
- **`confirmation_store.py`** — `ConfirmationStore` using SQLAlchemy with a `confirmations` table. Provides `mark_committing_once` as an idempotency lock to prevent duplicate Notion writes.
- **`models.py`** — `ConfirmationRecord`, `ConfirmationStatus` (enum: `PENDING_REVIEW → COMMITTING → COMMITTED / REJECTED / FAILED`), `IntakeResult`.
- **`database_urls.py`** — converts a single `DATABASE_URL` into the sync driver (psycopg/sqlite) for `ConfirmationStore` and the async driver (asyncpg/aiosqlite) for ADK `DatabaseSessionService`.

### 3. Integrations Layer — `integrations/`

External system connections. Telegram provides three entry points that all share the same handlers; Notion is the write target for approved drafts.

- **`telegram/`** — `bot.py` (local polling), `webhook_dev.py` (local webhook server), `lambda_handlers.py` (deployed: webhook receives and enqueues to SQS, worker dequeues and processes). `handlers.py` contains the shared `handle_message` and `handle_callback` logic. `webhook.py` has `TelegramUpdateProcessor` and webhook validation. `parsing.py` extracts `IncomingContext` from Telegram messages. `rendering.py` builds inline keyboards and Markdown messages. `state.py` persists revision state and deduplicates updates.
- **`notion/writer.py`** — `NotionWriter` creates project pages and task pages in Notion databases, with default templates, status defaults, priority, tags, and project-task relations.

The integrations are detailed in [Telegram & Notion Integrations](../integrations/telegram-and-notion.md).

## How Layers Connect

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    subgraph "Telegram Entry Points"
        A1["bot.py<br/>(polling)"]
        A2["webhook_dev.py<br/>(local webhook)"]
        A3["lambda_handlers.py<br/>(deployed)"]
    end

    subgraph "Handlers"
        H["handlers.py<br/>handle_message / handle_callback"]
    end

    subgraph "Application"
        IS["IntakeService<br/>start_intake / resume_intake / finalize_approval"]
        CS["ConfirmationStore<br/>(SQLAlchemy)"]
    end

    subgraph "Agent (Google ADK)"
        WF["Workflow<br/>draft → review → approve/reject/revise"]
        DS["DatabaseSessionService<br/>(ADK sessions)"]
    end

    subgraph "Notion"
        NW["NotionWriter<br/>create_project_with_tasks"]
    end

    A1 --> H
    A2 --> H
    A3 --> H
    H -->|"start_intake / resume_intake"| IS
    IS -->|"runs workflow"| WF
    WF -->|"persists sessions"| DS
    IS -->|"persists confirmation"| CS
    IS -->|"on approval"| NW
```

## Key Design Decisions

- **Human-in-the-loop via ADK interrupts, not chat turns.** The workflow pauses at `request_human_review` by emitting a `RequestInput` interrupt. `IntakeService.resume_intake` replays the user's response into the paused workflow, allowing the agent to route to approved/rejected/revise.
- **Dual database drivers from one URL.** `database_urls.py` derives both sync (for `ConfirmationStore`) and async (for ADK `DatabaseSessionService`) connection strings from a single `DATABASE_URL`, supporting SQLite locally and Postgres/Supabase in production.
- **Idempotent Notion commits.** `mark_committing_once` atomically transitions a confirmation from `PENDING_REVIEW` to `COMMITTING`, preventing duplicate writes if `finalize_approval` is called multiple times.
- **Three Telegram entry points, shared handlers.** All three entry points (`bot.py`, `webhook_dev.py`, `lambda_handlers.py`) build the same `Application` via `build_application` and use the same `handle_message` / `handle_callback` handlers.
- **SQS queue in production.** The Lambda webhook handler validates and enqueues; the Lambda worker handler dequeues and processes. This decouples Telegram's webhook timeout from the potentially slow agent workflow.

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/agents/intake_triage/agent.py` | Agent and workflow assembly |
| `src/backlog_tamer/agents/intake_triage/workflow.py` | ADK workflow graph definition |
| `src/backlog_tamer/application/intake_service.py` | Core orchestration service |
| `src/backlog_tamer/application/confirmation_store.py` | Confirmation persistence with idempotency |
| `src/backlog_tamer/integrations/telegram/handlers.py` | Shared Telegram message/callback handlers |
| `src/backlog_tamer/integrations/telegram/bot.py` | `build_application` + polling entry point |
| `src/backlog_tamer/integrations/notion/writer.py` | Notion API writer |
| `src/backlog_tamer/config.py` | Settings model (pydantic-settings) |

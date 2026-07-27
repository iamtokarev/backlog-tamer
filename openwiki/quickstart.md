---
type: Quickstart
title: Backlog Tamer Quickstart
description: Telegram-first personal learning intake system that turns messy inputs into structured Notion backlog items via an AI triage agent with human-in-the-loop approval.
tags: [entrypoint, overview]
timestamp: 2025-01-20T00:00:00Z
---

# Backlog Tamer

Backlog Tamer is a Telegram bot that turns messy learning and project inputs — links, notes, ideas — into structured Notion backlog items. An AI agent drafts a project/task proposal, a human approves, rejects, or revises it via Telegram inline keyboards, and the approved result is written to Notion.

The product sits between curiosity and execution: Telegram is the low-friction inbox, the agent is the triage layer, and Notion is the system of record.

## Core Loop

1. **Capture** — user sends a link, note, or idea to the Telegram bot.
2. **Interpret** — the agent fetches URLs when useful, classifies the input, and drafts a `ProjectDraft` with grounding metadata.
3. **Propose** — the bot replies with the structured draft and inline-keyboard buttons (Approve / Reject / Revise / quick-edit pickers for priority, intent, type).
4. **Confirm** — user taps a button, adjusts fields with quick-edit pickers, or sends revision feedback.
5. **Commit** — on approval, the draft is written to Notion as a project page with task sub-pages. Duplicate URLs are detected and offered for merge. Failed saves can be retried.
6. **Reuse** — the user can undo a commit (archives Notion pages) or add tasks to an existing duplicate project, then reviews and acts from a cleaner Notion backlog.

## Tech Stack

- **Python 3.12+** with `uv` for dependency management
- **Google ADK** (≥2.4.0) — agent workflow with interrupts for human-in-the-loop
- **LiteLLM + OpenAI** — LLM backend for the drafting agent (default model: `gpt-5.6-luna`)
- **python-telegram-bot** (v22) — Telegram polling, webhook, and Lambda handlers
- **Notion API** — writes project + task pages via `httpx`
- **SQLAlchemy** (async + sync) — durable confirmation state (SQLite locally, Postgres/Supabase deployed)
- **LangSmith** — optional tracing
- **Terraform + AWS Lambda** — production deployment (SQS-queued webhook → worker pattern)

## Quick Commands

```sh
make run                                              # Run the polling bot locally
make webhook-dev PUBLIC_URL=https://your-ngrok-url    # Run local webhook server
make test                                             # uv run pytest -v
make lint                                             # uv run ruff check
make format-check                                     # CI enforces this
```

## Configuration

Copy `.env.example` to `.env` and fill in the required values. Settings use `pydantic-settings` with `__` as the nested delimiter (e.g. `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token`). See `src/backlog_tamer/config.py` for the full settings model.

Required env vars: `AGENT__OPENAI_API_KEY`, `TELEGRAM__BOT_TOKEN`, `TELEGRAM__ALLOWED_USER_ID`, `NOTION_TOKEN`, `NOTION_PROJECTS_DATABASE_ID`, `NOTION_TASKS_DATABASE_ID`, `DATABASE_URL`.

## Documentation Sections

- [Architecture Overview](architecture/overview.md) — three-layer design, how the agent, application, and integrations connect
- [Intake Workflow](workflows/intake-flow.md) — end-to-end flow from Telegram message to Notion commit, including the ADK workflow graph and confirmation lifecycle
- [Data Model](domain/data-model.md) — `ProjectDraft`, `IncomingContext`, `ConfirmationRecord`, database tables, and Notion payload structures
- [Telegram & Notion Integrations](integrations/telegram-and-notion.md) — three Telegram entry points, handlers, webhook validation, Notion writer
- [Deployment & Operations](operations/deployment.md) — local dev, Docker, Terraform, CI/CD, secrets

## Backlog

- **URL fetch tool internals** (`src/backlog_tamer/agents/intake_triage/tools/fetch_url.py`) — SSRF protection, HTML/PDF parsing, X/Twitter oEmbed handling are not yet documented in detail. Deferred because the tool is well-tested and self-contained; a future run could document the security model and content extraction pipeline.
- **Product roadmap milestones B–D** (`documentation/milestone-b-smart-triage.md` through `milestone-d-operating-system.md`) — not yet documented. Deferred because the codebase only implements Milestone A; the milestone docs are planning text without corresponding code.
- **`test.ipynb`** — appears to be a scratch notebook. No documentation needed.

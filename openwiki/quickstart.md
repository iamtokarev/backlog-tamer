---
type: Quickstart
title: Backlog Tamer Quickstart
description: Telegram-first personal learning intake system that turns messy inputs into structured Notion backlog items via decision-model Triage with human-in-the-loop approval.
tags: [entrypoint, overview]
timestamp: 2026-11-06T00:00:00Z
---

# Backlog Tamer

Backlog Tamer is a Telegram bot that turns messy learning and project inputs — links, notes, ideas — into structured Notion backlog items. Decision models draft a `ProjectDraft`, a human approves, rejects, quick-edits, or revises it via Telegram inline keyboards, and the approved result is written to Notion.

The product sits between curiosity and execution: Telegram is the low-friction inbox, [Triage](workflows/intake-flow.md) is the decision layer, and Notion is the system of record.

## Core Loop

1. **Capture** — user sends a link, note, or idea to the Telegram bot.
2. **Read** — the [Source Reader](integrations/source-reader.md) fetches the first link through Browserbase into a `Grounding` (title, key points, canonical URL).
3. **Triage** — `Triage.classify` asks a decision model typed questions (type, then intent/tags/priority with the type known) and one small LLM call writes the title, in parallel. No summary is written and no tasks are proposed.
4. **Propose** — the bot replies with the structured draft and inline-keyboard buttons (Approve / Reject / Revise / quick-edit pickers for priority, intent, project type).
5. **Confirm** — user taps a button, adjusts fields with quick-edit pickers, or sends revision feedback. A revision routes the feedback to the fields it is about and re-decides only those.
6. **Commit** — on approval, the draft is written to Notion as a project page. Duplicate URLs are detected and offered for merge. Failed saves can be retried.
7. **Reuse** — the user can undo a commit (archives the Notion page) and then reviews and acts from a cleaner Notion backlog.

## Tech Stack

- **Python 3.12+** with `uv` for dependency management, orchestrated by `mise`
- **Decision models via OpenRouter** — `Triage` asks a decision model typed questions (`POST /api/alpha/decisions`) and one title LLM call (`/v1/chat/completions`); no LLM drafting agent, no ADK, no engine switch (see [ADR 0001](https://github.com/iamtokarev/backlog-tamer/blob/main/docs/adr/0001-triage-by-decision-models.md))
- **Browserbase + Stagehand** — source reading via managed Fetch API and hosted browser rendering; the `SourceReader` is called directly by `IntakeService` (see [Source Reader](integrations/source-reader.md))
- **python-telegram-bot** (v22) — Telegram polling, webhook, and Lambda handlers
- **Notion API** — writes project pages via `httpx` (no task pages; drafts carry no tasks)
- **SQLAlchemy** (sync) — durable confirmation state (SQLite locally, Postgres/Supabase deployed)
- **Terraform + AWS Lambda** — production deployment (SQS-queued webhook → worker pattern)

## Quick Commands

```sh
mise install && mise run setup     # Install locked dependencies
cp .env.example .env               # fill in keys
mise run bot                       # Run the polling bot locally
mise run webhook -- --public-url https://your-ngrok-url   # Local webhook server
mise run check                     # format-check + lint + test (same as CI)
mise run test                      # uv run --locked pytest -q
mise run eval                      # Measure Triage on the golden set (see evals/README.md)
```

`mise tasks` lists everything. Commands map to `mise.toml`; there is no `Makefile`.

## Configuration

Copy `.env.example` to `.env` and fill in the required values. Settings use `pydantic-settings` with `__` as the nested delimiter (e.g. `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token`). See `src/backlog_tamer/config.py` for the full settings model.

Required env vars: `OPENROUTER_API_KEY`, `BROWSERBASE_API_KEY`, `TELEGRAM__BOT_TOKEN`, `TELEGRAM__ALLOWED_USER_ID`, `NOTION_TOKEN`, `NOTION_PROJECTS_DATABASE_ID`, `DATABASE_URL`.

Optional: `TRIAGE__DECISION_MODEL` (default `perplexity/pplx-decider-v1.3:free`; an OpenRouter decision-model slug), `TRIAGE__TITLE_MODEL` (default `openai/gpt-6-luna`; an OpenRouter slug for the title LLM call), `TELEGRAM__WEBHOOK_SECRET`, `NOTION_API_VERSION` (default `2022-06-28`).

> The free decision model (`perplexity/pplx-decider-v1.3:free`) is temporary and ends around mid-December 2026. The deploy healthcheck probes both models, so a dead slug fails the deploy; switching is one setting (`TRIAGE__DECISION_MODEL`), and `mise run eval -- --model <slug>` measures a replacement first.

## Model Validation

`tests/test_config.py` covers settings loading: `OPENROUTER_API_KEY` and `BROWSERBASE_API_KEY` are both required (blank/whitespace fails), environment variables override `.env`, and the defaults resolve to `perplexity/pplx-decider-v1.3:free` / `openai/gpt-6-luna`. `tests/test_decision_client.py` exercises the OpenRouter transport with a mocked `httpx` client: it asserts the decisions endpoint is `https://openrouter.ai/api/alpha/decisions`, the title call targets `/v1/chat/completions` with `reasoning: {"effort": "none"}` and `provider: {"require_parameters": True}`, `Bearer` auth, retry behavior on 429/502/503/524/529, and usage logging. `tests/test_triage.py` drives `Triage.classify`/`revise` through a `FakeClient` that returns scripted answers and asserts the right questions are asked in the right order, routing only re-decides routed fields, and a type change re-asks intent.

`mise run eval` measures the production `Triage` on the golden set exported from Notion (see [evals/README.md](https://github.com/iamtokarev/backlog-tamer/blob/main/evals/README.md)). Run it before changing a model, a criterion, or a threshold.

## Documentation Sections

- [Architecture Overview](architecture/overview.md) — three-layer design: Triage, application orchestration, and integrations
- [Intake Workflow](workflows/intake-flow.md) — end-to-end flow from Telegram message to Notion commit, including the confirmation lifecycle
- [Data Model](domain/data-model.md) — `ProjectDraft`, `IncomingContext`, `Grounding`, `ConfirmationRecord`, database tables, and Notion payload structures
- [Telegram & Notion Integrations](integrations/telegram-and-notion.md) — three Telegram entry points, handlers, webhook validation, Notion writer
- [Source Reader](integrations/source-reader.md) — Browserbase + Stagehand source reading, the extraction escalation ladder, SSRF validation, and `FetchedUrl.to_grounding`
- [Deployment & Operations](operations/deployment.md) — local dev, Docker, Terraform, CI/CD, secrets, mise tasks

## Backlog

- **Evals pipeline** (`evals/`) — the golden-set export, classify/revise/titles stages, and the disk-caching `DecisionClient` are documented in `evals/README.md` but not yet as a wiki page. Source anchor: `evals/{run,golden,metrics,cases,caching}.py`. Promote when a model swap or threshold change is in flight.
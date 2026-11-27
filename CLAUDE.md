## What this is

Backlog Tamer is a Telegram bot that turns messy learning/project inputs (links, notes) into structured Notion backlog items. Decision models draft a Project, a human approves, quick-edits or revises it via Telegram, and the approved result is written to Notion. `docs/adr/0001-triage-by-decision-models.md` records why it is not an LLM agent.

## Commands

Use `mise tasks` to discover local commands and `mise run check` to run the Python CI checks. uv manages Python dependencies. Keep `uv.lock` in sync when changing them.

`mise run eval` measures Triage on the local golden set (`mise run eval:golden` builds it from Notion; see `evals/README.md`). Run it before changing a model, a criterion or a threshold. `uv run python -m backlog_tamer.dev.run_triage URL [--note TEXT] [--revise FEEDBACK]...` runs one Intake and its Revisions without Telegram, a database or Notion writes.

Requires a `.env` file (see `.env.example`). Settings use pydantic-settings with `__` as the nested delimiter (e.g. `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token`); see `src/backlog_tamer/config.py`.

## Releases and deploys

Merging to `main` does **not** deploy. Production ships from releases:

1. Push conventional commits (`feat:`, `fix:`, …) to `main`. `release.yml` keeps a standing release PR open that bumps `pyproject.toml`, relocks `uv.lock`, and writes `CHANGELOG.md`.
2. Merge the release PR. release-please tags `vX.Y.Z` and cuts a GitHub release, CI re-runs as a gate, then `deploy.yml` builds the arm64 image, pushes it to ECR tagged `vX.Y.Z`, applies Terraform, and smoke-tests both Lambdas.

The smoke test invokes the worker with `{"healthcheck": true}` and asserts the reported version matches the tag, so a deploy that silently didn't take fails the job. `_healthcheck` in `lambda_handlers.py` validates settings, eagerly imports the source reader, calls the decision and title models once each, and checks the Notion Projects schema. It reports `models`, and the deploy asserts both are present, so a retired model slug or bad key fails the deploy instead of the first capture.

Roll back by dispatching the Deploy workflow with a previous tag:

```sh
gh workflow run deploy.yml -f version=v0.1.1
```

The image is already in ECR, so the build is skipped and only `terraform apply` runs. The ECR lifecycle policy keeps the last 10 `v`-tagged images as rollback targets. Version bumps are release-please's job — don't edit `version` in `pyproject.toml` by hand.

`bootstrap-sha` in `release-please-config.json` is the history boundary for the first release (the repo had no `v*` tags). It can be dropped once `v0.2.0` exists.

The first release cut `backlog-tamer-v0.2.0`, not `v0.2.0`: release-please prefixes the component name unless `include-component-in-tag` is false, which it now is. That one tag keeps its old name, so rolling back to it needs the full string:

```sh
gh workflow run deploy.yml -f version=backlog-tamer-v0.2.0
```

It is also outside the ECR lifecycle rule that retains `v`-prefixed releases, so it is only held by the catch-all "keep last 20 images" rule. Releases from `v0.3.0` on are protected normally.

Rolling back past the decision-triage release to `v0.8.0` (the last agent release) works, but Reviews created after the cutover cannot be revised or approved there: v0.8.0 resumes agent sessions they never had. The legacy `confirmations` columns and the `AGENT__*` / `NOTION_TASKS_DATABASE_ID` secret keys are kept for that until the rollback window closes.

## Notion database schema

Property names live in `integrations/notion/writer.py` as module constants —
rename a column in Notion and it is a one-line change there.

Drafts carry no Tasks, so only the Projects database is written. Required (the deploy healthcheck fails without them):

- **Projects**: `Project name` (title), `Status`, `Priority`, `Tags` (multi-select), `Summary` (rich text)

Optional, listed in `OPTIONAL_PROJECT_PROPERTIES`.
The writer reads each database's schema once and silently drops properties that
do not exist, so these can be added whenever — until then the feature is just
absent:

- **Projects**: `Source` (url), `Project type` (select, formerly `Type`), `Intent` (select), `Captured` (date)

That silence is how `Project type` and `Intent` went missing from the live
workspace for months while Triage kept classifying into them. Run
`scripts/migrate_notion_schema.py` (dry-run by default, `--apply` to write) to
add any of the optional columns and backfill them from the page bookmark,
`created_time`, and the legacy type/intent values still in `Tags`.

Duplicate detection queries Projects by `Source`, so without that column every
send creates a new project. `worker_handler` with `{"healthcheck": true}`
reports `skipped_notion_properties` and, for gaps that disable a feature rather
than just blanking a field, `degraded_capabilities`.

## Architecture

Under `src/backlog_tamer/`:

- **`domain.py`** — the shared types: `IncomingContext`, `Grounding` (what was read from the source), `ProjectDraft` (no Tasks; `with_edits` re-validates), and `fold_tag`, the single tag-spelling rule.

- **`triage/`** — `Triage`, a stateless deep module with two operations: `classify(context, grounding, vocabulary)` and `revise(context, grounding, draft, feedback, vocabulary)`, both returning a `ProjectDraft`. Classify asks the decision model for the project type first, then intent, tags and (only when there is a note) priority with the type known; one title call to a small LLM runs in parallel. Revise first routes the feedback to the fields it is about and decides only those again; the title is rewritten only when the name is routed. A missing answer keeps the current value or the default. `questions.py` holds the Label set criteria, question builders and answer readers; every question carries the prompt-injection guard. `client.py` is `DecisionClient` (OpenRouter `POST /api/alpha/decisions` in 128-question chunks, plus the title chat call, with retries) and logs one `decision_triage_call` line per request. Models come from `TRIAGE__DECISION_MODEL` (default `perplexity/pplx-decider-v1.3:free`) and `TRIAGE__TITLE_MODEL` (default `openai/gpt-6-luna`); `OPENROUTER_API_KEY` is required.

- **`application/`** — orchestration, independent of Telegram. `IntakeService` (`intake_service.py`) reads the source, calls `Triage`, and persists a `ConfirmationRecord` via `ConfirmationStore` (SQLAlchemy, `confirmations` table). `start_intake` reads the first link and classifies; `revise` and `refetch` re-run Triage and re-apply the Quick edits, which last for the whole Review; `approve` / `reject` run no model call. `finalize_approval` uses `mark_committing_once` as an idempotency lock before writing to Notion (statuses: PENDING_REVIEW → COMMITTING → COMMITTED / REJECTED / FAILED). The legacy agent columns stay mapped with empty defaults for the v0.8.0 rollback. `database_urls.py` (top level) converts one `DATABASE_URL` into the sync driver (psycopg/sqlite) — support both SQLite (local) and Postgres/Supabase (deployed) when touching persistence.

- **`integrations/`** — `telegram/` has three entry points sharing the same handlers: `bot.py` (local polling), `webhook_dev.py` (local webhook server), and `lambda_handlers.py` (deployed: `webhook_handler` validates the secret/allowed user and enqueues raw updates to SQS; `worker_handler` consumes SQS records and runs the real processing, loading secrets from AWS Secrets Manager). `notion/writer.py` creates the project page. Review actions arrive as inline-keyboard callbacks handled in `handlers.py`.

- **`evals/`** (repo root, not in the image) — drives the production `Triage` through a disk-caching `DecisionClient`, so re-scoring is free. Stages: classify (type / intent / priority accuracy, tag P/R/F1, latency, cost), revise (comment applied, unrelated field changed) and titles (judged against the approved Notion title). The repo is public: `evals/data/` (golden set, cache, reports) is gitignored, and only aggregate numbers go anywhere else.

Source reading lives in `integrations/source_reader.py`. Its `SourceReader.read` interface returns a `FetchedUrl`, and `FetchedUrl.to_grounding()` turns it into a `Grounding`. All retrieval uses Browserbase: raw Fetch resolves redirects and content types, Fetch JSON extracts HTML, and hosted Stagehand automatically renders insufficient or known dynamic sources. YouTube/X oEmbed and PDF parsing retain their specialized interpretation. SDK clients and browser sessions are owned by each read because Lambda creates a new event loop for each invocation.

`BROWSERBASE_API_KEY` is required locally and in the deployed runtime secret. Stagehand uses Browserbase Model Gateway for extraction. URL identity helpers shared with maintenance scripts live in `source_urls.py`. Local public-address validation covers inputs and Fetch redirects; Browserbase owns remote DNS and egress.

## Infrastructure

Terraform in `infra/terraform/` (Lambda ×2, SQS + DLQ, ECR, Secrets Manager). `scripts/build_and_push_image.sh` builds the Lambda container image. Both Lambdas run from the same image; the handler name selects webhook vs worker.

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

Run `openwiki code --update` to refresh the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

## Agent skills

### Issue tracker

Issues and specs live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), recorded as a `Status:` line in each issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

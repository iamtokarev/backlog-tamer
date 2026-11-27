---
type: Operations
title: Deployment and Operations
description: >-
  Local development setup with mise, Docker container build, AWS Lambda
  deployment with Terraform (SQS, ECR, Secrets Manager), CI/CD via GitHub
  Actions, and runtime secret management.
tags: [operations, deployment, ci-cd, terraform, aws]
timestamp: 2026-11-06T00:00:00Z
---

# Deployment and Operations

## Local Development

### Prerequisites

- [mise](https://mise.jdx.dev) — installs the pinned `uv`, which manages Python 3.12+ and dependencies
- A `.env` file (copy from `.env.example`)

### mise Tasks

Commands are defined in `mise.toml`; there is no `Makefile`. `mise tasks` lists everything, and `mise -E infra tasks` adds the Terraform tasks.

| Command | Description |
|---------|-------------|
| `mise run setup` | `uv sync --locked` — install locked dependencies |
| `mise run bot` | Run the Telegram polling bot |
| `mise run webhook -- --public-url https://...` | Run the local webhook server (for ngrok testing) |
| `mise run test` | `uv run --locked pytest -q` (pass a file or `-k <pattern>` to narrow) |
| `mise run lint` | `uv run --locked ruff check` |
| `mise run lint:fix` | `uv run --locked ruff check --fix` |
| `mise run format` | `uv run --locked ruff format` |
| `mise run format:check` | `uv run --locked ruff format --check` (CI enforces) |
| `mise run check` | `format:check` + `lint` + `test` (the same checks CI runs) |
| `mise run webhook:info` | Inspect the current Telegram webhook config |
| `mise run webhook:clear` | Delete the Telegram webhook |
| `mise run eval:golden` | Export the golden set from Notion and read its sources |
| `mise run eval` | Measure Triage on the golden set (see `evals/README.md`) |

### Standalone Triage Dev

Run Triage without Telegram, a database, or Notion writes:

```sh
uv run --locked python -m backlog_tamer.dev.run_triage URL --note "why I saved it" --revise "rename it to X"
```

Reads the source, classifies, and applies each revision in order, printing every draft as JSON with per-step timings. See [Intake Workflow](../workflows/intake-flow.md).

### Database

Local default is SQLite (`sqlite:///backlog_tamer.db`). Set `DATABASE_URL` to a Postgres URL for production or Supabase testing. `database_urls.to_sync_database_url` derives the sync SQLAlchemy driver from the URL scheme.

## CI Pipeline

`.github/workflows/ci.yml` runs on every PR and push to `main` (and as a reusable `workflow_call`):

| Job | Steps |
|-----|-------|
| Python | `ruff format --check`, `ruff check`, `pytest -q` (all with `uv run --locked`) |
| Terraform | `terraform fmt -check`, `terraform init -backend=false`, `terraform validate` |
| Docker | `docker buildx build --platform linux/arm64 -t backlog-tamer:ci --load` (native arm64 runner) |

CI uses `uv --locked` — keep `uv.lock` in sync when changing dependencies.

## Production Deployment

### Architecture

```
Telegram → Lambda Function URL (webhook_handler)
                      ↓ validates + enqueues
                   SQS Queue ← DLQ (max 3 receives)
                      ↓
              Lambda (worker_handler) → IntakeService → Triage → Notion
```

Both Lambda functions use the same Docker image; the handler name (`webhook_handler` vs `worker_handler`) selects behavior.

### Docker Image

`Dockerfile` uses the AWS Lambda Python 3.12 base image, installs `uv`, and syncs dependencies with `--locked --no-dev --inexact`. The default CMD is the worker handler.

### Terraform Infrastructure

`infra/terraform/` defines:

| Resource | Purpose |
|----------|---------|
| `aws_ecr_repository.app` | Container image registry |
| `aws_ecr_lifecycle_policy.app` | Keep last 10 `v`-tagged images (rollback targets) |
| `aws_secretsmanager_secret.app` | Runtime secrets (JSON object with all env vars) |
| `aws_sqs_queue.updates` | Telegram update queue (visibility timeout = worker timeout + 30s) |
| `aws_sqs_queue.updates_dlq` | Dead-letter queue (14-day retention) |
| `aws_lambda_function.webhook` | Webhook receiver (512 MB, 15s timeout) |
| `aws_lambda_function.worker` | SQS consumer (1024 MB, 300s timeout) |
| `aws_lambda_function_url.webhook` | Public HTTPS endpoint for Telegram |
| `aws_cloudwatch_log_group.webhook` | Webhook logs (14-day retention) |
| `aws_cloudwatch_log_group.worker` | Worker logs (14-day retention) |

Lambda architecture defaults to `arm64`.

### Secrets

Runtime secrets are stored as a JSON object in AWS Secrets Manager. The Lambda `_load_runtime_secrets()` function reads the secret on cold start, gated by the `BACKLOG_TAMER_SECRET_ARN` environment variable that Terraform sets on both Lambda functions: if the ARN is absent it skips loading (local/dev runs have no secret), otherwise it calls `secretsmanager:GetSecretValue`, parses the `SecretString` as JSON, and sets each key-value as an environment variable via `os.environ.setdefault()`. After loading, it clears the `get_settings` LRU cache so `pydantic-settings` re-reads the new env vars. The IAM role is granted `secretsmanager:GetSecretValue` on the secret ARN only (`infra/terraform/main.tf`).

The secret JSON should contain every required key from `.env.example` (including `OPENROUTER_API_KEY`, `BROWSERBASE_API_KEY`, the Telegram and Notion settings, and `DATABASE_URL`); optional settings absent from the JSON retain their `Settings` defaults. Loading uses `os.environ.setdefault()`, so a key already present in the Lambda environment takes precedence over the secret value — the secret fills only keys that are unset. The required model keys are `OPENROUTER_API_KEY` (direct OpenAI credentials are no longer used); `TRIAGE__DECISION_MODEL` and `TRIAGE__TITLE_MODEL` are optional OpenRouter slugs (defaults `perplexity/pplx-decider-v1.3:free` and `openai/gpt-6-luna`). The required source-reading key is `BROWSERBASE_API_KEY`. The local `.env` file is not shipped to Lambda.

### Release Workflow

`.github/workflows/release.yml` owns the release lifecycle. On every push to `main`, [release-please](https://github.com/googleapis/release-please) maintains a standing release PR that bumps `pyproject.toml` and writes `CHANGELOG.md` from conventional commits. Merging that PR cuts a tag and GitHub release, which triggers CI as a reusable workflow gate and then deploys:

1. **release-please** — creates/updates the release PR. Configured via `release-please-config.json` (release type `python`, `include-v-in-tags`, `include-component-in-tag: false` so tags are `v0.8.0` not `backlog-tamer-v0.8.0`, bootstrap SHA `8712113`). Version tracked in `.release-please-manifest.json` (currently `0.8.0`).
2. **relock** — re-runs `uv lock` on the release PR branch to keep `uv.lock` in sync with the version bump.
3. **CI** — re-runs the full CI suite against the release commit as a synchronous gate.
4. **Deploy** — invoked as a reusable `workflow_call` with the release tag as `version` input.

### Deploy Workflow

`.github/workflows/deploy.yml` is invoked by the release workflow (`workflow_call`) or manually (`workflow_dispatch`). Both paths require a `version` input (e.g. `v0.8.0`). Deploys are release-driven:

1. Configure AWS credentials via OIDC role (`AWS_ROLE_ARN`).
2. Checkout the release tag (`ref: ${{ inputs.version }}`).
3. Initialize Terraform with S3 backend.
4. Resolve ECR repository URL and name from Terraform output.
5. Check whether the release image already exists in ECR (by tag). If it does, skip the build — this makes **rollbacks** fast (only `terraform apply` runs).
6. If the image is missing, build and push to ECR tagged with both `$VERSION` and `sha-$GITHUB_SHA`.
7. `terraform apply -auto-approve` with `image_tag=$VERSION`.
8. **Post-deploy smoke tests:**
   - **Worker healthcheck** — invokes the worker Lambda with `{"healthcheck": true}`, which eagerly imports `source_reader`, calls `Triage.from_settings(settings).probe()` (which calls the configured decision and title models once each, so a retired slug or bad `OPENROUTER_API_KEY` fails the deploy), probes the Notion Projects schema and raises if required properties are missing, asserts the deployed version matches the release tag, and reports `models`, `skipped_notion_properties`, and `degraded_capabilities` (a missing `Source` column surfaces as `["duplicate-detection"]`). The deploy asserts both models are reported. See [Telegram and Notion Integrations](../integrations/telegram-and-notion.md) for the healthcheck implementation.
   - **Webhook auth rejection** — sends an unsigned request to the webhook Lambda and asserts a `403` response. A `503` would mean `TELEGRAM__WEBHOOK_SECRET` is not configured; a `200` would mean the webhook is accepting requests without validation.

Terraform outputs `webhook_function_name` and `worker_function_name` for the smoke tests.

Requires a `production` GitHub environment with variables: `AWS_REGION`, `AWS_ROLE_ARN`, `TF_STATE_BUCKET`.

### Rollback

Roll back by dispatching the Deploy workflow with a previous tag — the image already exists in ECR, so the build is skipped and only `terraform apply` runs:

```sh
gh workflow run deploy.yml -f version=v0.8.0
```

Rolling back past the decision-triage cutover to `v0.8.0` (the last agent release) works, but Reviews created after the cutover cannot be revised or approved there: v0.8.0 resumes agent sessions they never had. The legacy `confirmations` columns and the `AGENT__*` / `NOTION_TASKS_DATABASE_ID` secret keys are kept for that until the rollback window closes.

### Manual Build and Push

```sh
./scripts/build_and_push_image.sh
```

This script reads the ECR repo URL from Terraform output, logs into ECR, builds the image for `linux/arm64`, and pushes it. Requires AWS CLI, Docker, and Terraform.

### Terraform Backend

Copy `infra/terraform/backend.hcl.example` to `backend.hcl` and set the S3 bucket name. Initialize once:

```sh
terraform -chdir=infra/terraform init -backend-config=backend.hcl -migrate-state
```

### Post-Deployment

After deployment, register the Telegram webhook to the Lambda Function URL (output as `webhook_function_url`) with the configured webhook secret. The deploy does not register it, so after `mise run webhook:clear` (for local polling) set it again the same way.

## Key Operations Notes

- **Update deduplication** — The Lambda worker uses `TelegramStateStore.record_update_once` to skip duplicate `update_id` values from SQS retries.
- **NullPool for external poolers** — When using Supabase's transaction-mode pooler (`pooler.supabase.com` in the URL), `ConfirmationStore` and `TelegramStateStore` use `NullPool` via `uses_external_pooler()` in `database_urls.py`. (The asyncpg statement-cache workaround for the ADK session service is gone with ADK.)
- **Free decision model is temporary** — `perplexity/pplx-decider-v1.3:free` ends around mid-December 2026. The deploy healthcheck probes both models, so a dead slug fails the deploy; switching is one setting (`TRIAGE__DECISION_MODEL`), and `mise run eval -- --model <slug>` measures a replacement first.
- **Secrets Manager rollout** — Before a model or source-reader change reaches production, add `OPENROUTER_API_KEY` and `BROWSERBASE_API_KEY` to the runtime JSON secret in Secrets Manager. The worker healthcheck validates the full `Settings` model (which requires non-blank `OPENROUTER_API_KEY` and `BROWSERBASE_API_KEY`) and calls `Triage.probe()` (which calls both models), but does not open a browser session, so verify a real intake and review after deployment. Keep the previous image's credentials available during the rollback window.

## Source References

| File | Purpose |
|------|---------|
| `Dockerfile` | Lambda container image build |
| `mise.toml` | Local dev tasks (setup, bot, webhook, test, lint, eval) |
| `.github/workflows/ci.yml` | CI pipeline (reusable via `workflow_call`) |
| `.github/workflows/deploy.yml` | CD pipeline (release-driven, smoke tests) |
| `.github/workflows/release.yml` | Release-please lifecycle, relock, CI gate, deploy |
| `release-please-config.json` | release-please configuration |
| `.release-please-manifest.json` | release-please version manifest |
| `infra/terraform/main.tf` | AWS resources |
| `infra/terraform/variables.tf` | Terraform variables |
| `infra/terraform/outputs.tf` | Terraform outputs |
| `scripts/build_and_push_image.sh` | Manual image build/push |
| `src/backlog_tamer/integrations/telegram/lambda_handlers.py` | Lambda handlers and secret loading |
| `src/backlog_tamer/dev/run_triage.py` | Standalone Triage runner for dev |
| `scripts/migrate_notion_schema.py` | One-time Notion schema migration (add optional columns + backfill) |

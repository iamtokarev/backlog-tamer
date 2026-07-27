---
type: Operations
title: Deployment and Operations
description: >-
  Local development setup, Docker container build, AWS Lambda deployment with
  Terraform (SQS, ECR, Secrets Manager), CI/CD via GitHub Actions, and runtime
  secret management.
tags: [operations, deployment, ci-cd, terraform, aws]
timestamp: 2025-01-20T00:00:00Z
---

# Deployment and Operations

## Local Development

### Prerequisites

- Python 3.12+
- `uv` for dependency management
- A `.env` file (copy from `.env.example`)

### Make Commands

| Command | Description |
|---------|-------------|
| `make run` | Run the Telegram polling bot |
| `make webhook-dev PUBLIC_URL=https://...` | Run local webhook server (for ngrok testing) |
| `make test` | `uv run pytest -v` |
| `make lint` | `uv run ruff check` |
| `make lint-fix` | `uv run ruff check --fix` |
| `make format` | `uv run ruff format` |
| `make format-check` | `uv run ruff format --check` (CI enforces) |
| `make webhook-info` | Inspect current Telegram webhook config |
| `make webhook-clear` | Delete Telegram webhook |

### Standalone Agent Dev

Run the agent workflow without Telegram or database:

```sh
uv run python -m backlog_tamer.dev.run_intake_workflow "some text" --link https://example.com --review-reply approve
```

Uses `InMemorySessionService` — no persistence needed. Useful for iterating on prompts and schemas.

### Database

Local default is SQLite (`backlog_tamer.db`). Set `DATABASE_URL` to a Postgres URL for production or Supabase testing. The `database_urls.py` module auto-derives the correct sync/async driver pair from the URL scheme.

## CI Pipeline

`.github/workflows/ci.yml` runs on every PR and push to `main`:

| Job | Steps |
|-----|-------|
| Python | `ruff format --check`, `ruff check`, `pytest -q` (all with `--locked`) |
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
              Lambda (worker_handler) → IntakeService → Notion
```

Both Lambda functions use the same Docker image; the handler name (`webhook_handler` vs `worker_handler`) selects behavior.

### Docker Image

`Dockerfile` uses the AWS Lambda Python 3.12 base image, installs `uv`, and syncs dependencies with `--locked --no-dev --inexact`. The default CMD is the worker handler.

### Terraform Infrastructure

`infra/terraform/` defines:

| Resource | Purpose |
|----------|---------|
| `aws_ecr_repository.app` | Container image registry |
| `aws_ecr_lifecycle_policy.app` | Keep last 5 images |
| `aws_secretsmanager_secret.app` | Runtime secrets (JSON object with all env vars) |
| `aws_sqs_queue.updates` | Telegram update queue (visibility timeout = worker timeout + 30s) |
| `aws_sqs_queue.updates_dlq` | Dead-letter queue (14-day retention) |
| `aws_lambda_function.webhook` | Webhook receiver (512 MB, 15s timeout) |
| `aws_lambda_function.worker` | SQS consumer (1024 MB, 300s timeout) |
| `aws_lambda_function_url.webhook` | Public HTTPS endpoint for Telegram |
| `aws_cloudwatch_log_group.webhook` | Webhook logs (14-day retention) |
| `aws_cloudwatch_log_group.worker` | Worker logs (14-day retention) |

Lambda architecture defaults to `arm64`. The worker has a minimum SQS concurrency of 2 (AWS requirement).

### Secrets

Runtime secrets are stored as a JSON object in AWS Secrets Manager. The Lambda `_load_runtime_secrets()` function reads the secret on cold start and sets each key-value as an environment variable via `os.environ.setdefault()`. After loading, it clears the `get_settings` LRU cache so `pydantic-settings` re-reads the new env vars.

The secret JSON should contain all keys from `.env.example` (e.g. `AGENT__OPENAI_API_KEY`, `TELEGRAM__BOT_TOKEN`, etc.).

### Release Workflow

`.github/workflows/release.yml` owns the release lifecycle. On every push to `main`, [release-please](https://github.com/googleapis/release-please) maintains a standing release PR that bumps `pyproject.toml` and writes `CHANGELOG.md` from conventional commits. Merging that PR cuts a tag and GitHub release, which triggers CI as a reusable workflow gate and then deploys:

1. **release-please** — creates/updates the release PR. Configured via `release-please-config.json` (release type `python`, `include-v-in-tags`, `include-component-in-tag: false` so tags are `v0.3.0` not `backlog-tamer-v0.3.0`, bootstrap SHA `8712113`). Version tracked in `.release-please-manifest.json` (currently `0.3.0`).
2. **relock** — re-runs `uv lock` on the release PR branch to keep `uv.lock` in sync with the version bump (release-please force-pushes the branch, so this commit is ephemeral and re-applied each run).
3. **CI** — re-runs the full CI suite against the release commit as a synchronous gate.
4. **Deploy** — invoked as a reusable `workflow_call` with the release tag as `version` input.

### Deploy Workflow

`.github/workflows/deploy.yml` is invoked by the release workflow (`workflow_call`) or manually (`workflow_dispatch`). Both paths require a `version` input (e.g. `v0.2.0`). The old `workflow_run` auto-trigger on CI pass has been removed — deploys are now release-driven:

1. Configure AWS credentials via OIDC role (`AWS_ROLE_ARN`).
2. Checkout the release tag (`ref: ${{ inputs.version }}`).
3. Initialize Terraform with S3 backend.
4. Resolve ECR repository URL and name from Terraform output.
5. Check whether the release image already exists in ECR (by tag). If it does, skip the build — this makes **rollbacks** fast (only `terraform apply` runs).
6. If the image is missing, build and push to ECR tagged with both `$VERSION` and `sha-$GITHUB_SHA`.
7. `terraform apply -auto-approve` with `image_tag=$VERSION`.
8. **Post-deploy smoke tests:**
   - **Worker healthcheck** — invokes the worker Lambda with `{"healthcheck": true}`, which eagerly imports agent and Notion modules, checks for missing extraction dependencies (`beautifulsoup4`, `pypdf`), and asserts the deployed version matches the release tag. See [Telegram and Notion Integrations](../integrations/telegram-and-notion.md) for the healthcheck implementation.
   - **Webhook auth rejection** — sends an unsigned request to the webhook Lambda and asserts a `403` response. A `200` would mean `TELEGRAM__WEBHOOK_SECRET` is unset and the public Function URL is unauthenticated.

Terraform outputs `webhook_function_name` and `worker_function_name` for the smoke tests.

Requires a `production` GitHub environment with variables: `AWS_REGION`, `AWS_ROLE_ARN`, `TF_STATE_BUCKET`.

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

After deployment, register the Telegram webhook to the Lambda Function URL (output as `webhook_function_url`) with the configured webhook secret.

## Key Operations Notes

- **Update deduplication** — The Lambda worker uses `TelegramStateStore.record_update_once` to skip duplicate `update_id` values from SQS retries. This is separate from the ADK session state.
- **NullPool and asyncpg statement cache for external poolers** — When using Supabase's transaction-mode pooler (port 6543, `pooler.supabance.com` in URL), `ConfirmationStore` and `TelegramStateStore` use `NullPool`, and the ADK `DatabaseSessionService` async engine gets `connect_args={"statement_cache_size": 0}`. This fixes asyncpg's "prepared statement does not exist" error behind PgBouncer, where each transaction lands on a different backend. Detection is via `uses_external_pooler()` in `database_urls.py`.
- **LangSmith tracing** — Enabled lazily in `IntakeService` when `LANGSMITH_API_KEY` is set. `Settings.export_to_env()` populates the LangSmith env vars from the settings model.
- **google-adk version** — Pinned to ≥2.4.0 because of a fix (commit `41409b4`) where `run_async` `state_delta` did not reach workflow nodes in earlier versions.

## Source References

| File | Purpose |
|------|---------|
| `Dockerfile` | Lambda container image build |
| `Makefile` | Local dev commands |
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
_tamer/integrations/telegram/lambda_handlers.py` | Lambda handlers and secret loading |

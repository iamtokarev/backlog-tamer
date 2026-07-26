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
| Docker | `docker buildx build --platform linux/amd64 -t backlog-tamer:ci --load` |

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

### Deploy Workflow

`.github/workflows/deploy.yml` triggers automatically when CI passes on `main` (`workflow_run` event) or manually (`workflow_dispatch`):

1. Configure AWS credentials via OIDC role (`AWS_ROLE_ARN`).
2. Initialize Terraform with S3 backend.
3. Resolve ECR repository URL from Terraform output.
4. Build and push Docker image to ECR (tagged with commit SHA).
5. `terraform apply -auto-approve` with the image tag.

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
- **NullPool for external poolers** — When using Supabase/external connection poolers, `ConfirmationStore` and `TelegramStateStore` use `NullPool` to avoid connection lifecycle conflicts. Detection is based on the database URL pattern.
- **LangSmith tracing** — Enabled lazily in `IntakeService` when `LANGSMITH_API_KEY` is set. `Settings.export_to_env()` populates the LangSmith env vars from the settings model.
- **google-adk version** — Pinned to ≥2.4.0 because of a fix (commit `41409b4`) where `run_async` `state_delta` did not reach workflow nodes in earlier versions.

## Source References

| File | Purpose |
|------|---------|
| `Dockerfile` | Lambda container image build |
| `Makefile` | Local dev commands |
| `.github/workflows/ci.yml` | CI pipeline |
| `.github/workflows/deploy.yml` | CD pipeline |
| `infra/terraform/main.tf` | AWS resources |
| `infra/terraform/variables.tf` | Terraform variables |
| `infra/terraform/outputs.tf` | Terraform outputs |
| `scripts/build_and_push_image.sh` | Manual image build/push |
| `src/backlog_tamer/integrations/telegram/lambda_handlers.py` | Lambda handlers and secret loading |

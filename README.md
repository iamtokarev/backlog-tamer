# Backlog Tamer

Backlog Tamer is a Telegram bot that turns messy learning or project inputs into structured Notion backlog items.

It accepts links or notes in Telegram, uses an agent workflow to draft a project/task proposal, asks for approval or revision, and writes the approved result to Notion.

## Capabilities

- Telegram bot input by polling locally or webhook in local/deployed mode.
- Agent-based intake triage for links, notes, and project ideas.
- Human approval flow before writing anything to Notion.
- Durable confirmation/update state using SQLite locally or Postgres/Supabase when deployed.
- AWS Lambda deployment with a lightweight webhook receiver and an SQS-backed worker.

## Local Development

Install dependencies with `uv`, then create a `.env` file with the required Telegram, OpenRouter, Notion, and database settings (see `.env.example`).

Model requests use OpenRouter through ADK's LiteLLM adapter:

```dotenv
OPENROUTER_API_KEY=your-openrouter-key
AGENT__MODEL=openai/gpt-6-luna
AGENT__REASONING_EFFORT=medium
```

The model and reasoning settings above are the defaults. `AGENT__MODEL` takes an OpenRouter model slug including its publisher, such as `openai/gpt-6-luna`; the adapter adds the `openrouter/` transport prefix. Select a model that supports function tools, structured output, and the configured reasoning effort (`none`, `low`, `medium`, or `high`).

An OpenRouter key is required. Direct OpenAI credentials are no longer used.

Run the polling bot:

```sh
make run
```

Run the local webhook server:

```sh
make webhook-dev PUBLIC_URL=https://your-ngrok-url
```

Useful local commands:

```sh
make test
make lint
make format-check
make webhook-info
make webhook-clear
```

## Deployment

Deployment uses:

- AWS Lambda for the Telegram webhook and worker.
- SQS plus DLQ for queued Telegram updates.
- ECR for the Lambda container image.
- Secrets Manager for runtime secrets.
- Supabase/Postgres for durable state.

Before releasing the OpenRouter migration, add `OPENROUTER_API_KEY` to the runtime JSON secret in Secrets Manager. Convert any existing `AGENT__MODEL` value to its publisher-qualified OpenRouter slug. The local `.env` file is not shipped to Lambda.

The worker healthcheck validates configuration and imports but does not authenticate with the model. Verify a real intake and review after deployment. Keep the previous image's OpenAI credential and model configuration available during the rollback window; remove the obsolete secret entries after it closes.

Build and push the image:

```sh
./scripts/build_and_push_image.sh
```

Apply infrastructure changes:

```sh
terraform -chdir=infra/terraform apply
```

After deployment, register the Telegram webhook to the Lambda Function URL with the configured webhook secret.

Automatic deployment runs from GitHub Actions after CI passes on `main`, and can also be triggered manually from the `Deploy` workflow. It expects a `production` GitHub environment with:

- `AWS_REGION`
- `AWS_ROLE_ARN`
- `TF_STATE_BUCKET`

For shared infrastructure state, copy `infra/terraform/backend.hcl.example` to `infra/terraform/backend.hcl`, set your S3 bucket name, then initialize once with:

```sh
terraform -chdir=infra/terraform init -backend-config=backend.hcl -migrate-state
```

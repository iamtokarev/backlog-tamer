# Setup

Everything you need to run Backlog Tamer locally and deploy it. For the architecture and internals, see [CLAUDE.md](../CLAUDE.md).

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A Telegram bot token and your Telegram user ID. Only that user is served.
- An [OpenRouter](https://openrouter.ai) API key for the drafting agent
- A [Browserbase](https://www.browserbase.com) API key for source reading
- A Notion integration token with access to your Projects and Tasks databases
- A database: SQLite is fine locally, Postgres in production

## Configure

```sh
cp .env.example .env
```

Settings use `__` as the nested delimiter, so `TELEGRAM__BOT_TOKEN` maps to `Settings.telegram.bot_token` (see `src/backlog_tamer/config.py`).

| Variable | | Notes |
|---|---|---|
| `TELEGRAM__BOT_TOKEN` | required | |
| `TELEGRAM__ALLOWED_USER_ID` | required | The only user the bot serves |
| `TELEGRAM__WEBHOOK_SECRET` | webhook only | Secret token Telegram sends with each update |
| `OPENROUTER_API_KEY` | required | |
| `AGENT__MODEL` | optional | Publisher-qualified OpenRouter slug, default `openai/gpt-6-luna` |
| `AGENT__REASONING_EFFORT` | optional | `none`, `low`, `medium` (default) or `high` |
| `BROWSERBASE_API_KEY` | required | Source reading |
| `NOTION_TOKEN` | required | Needed to write to Notion |
| `NOTION_PROJECTS_DATABASE_ID` | required | |
| `NOTION_TASKS_DATABASE_ID` | required | |
| `NOTION_API_VERSION` | optional | Default `2022-06-28` |
| `DATABASE_URL` | optional | Default `sqlite:///backlog_tamer.db`; Postgres URLs work too |
| `LANGSMITH_API_KEY` | optional | Turns on tracing. `LANGSMITH_PROJECT` and `LANGSMITH_ENDPOINT` tune it |

## Run locally

| Command | Does |
|---|---|
| `make run` | Telegram bot with local polling |
| `make webhook-dev PUBLIC_URL=https://…` | Local webhook server behind a tunnel such as ngrok |
| `make test` | Run the test suite |
| `make lint` / `make format-check` | ruff, the same checks CI runs |
| `make webhook-info` / `make webhook-clear` | Inspect or delete the Telegram webhook |

To iterate on the agent without Telegram, run the workflow on its own with in-memory sessions:

```sh
uv run python -m backlog_tamer.dev.run_intake_workflow
```

## Set up the Notion databases

```sh
uv run python scripts/migrate_notion_schema.py          # dry run: shows missing columns
uv run python scripts/migrate_notion_schema.py --apply  # creates them and backfills
```

The required and optional columns are listed in [CLAUDE.md](../CLAUDE.md#notion-database-schema).

## Deploy

Production runs on AWS Lambda (webhook receiver → SQS → worker), with infrastructure in [`infra/terraform`](../infra/terraform).

Releases deploy; merges to `main` don't. Merge the standing release-please PR and the tagged version is built, deployed and smoke-tested. To roll back, dispatch the Deploy workflow with an earlier tag:

```sh
gh workflow run deploy.yml -f version=v0.7.2
```

Before releasing a source-reader change, add `BROWSERBASE_API_KEY` to the existing runtime secret selected by Terraform's `runtime_secret_arn` or the Lambda's `BACKLOG_TAMER_SECRET_ARN`.

One-time setup: point the Telegram webhook at the webhook Lambda's Function URL, using `TELEGRAM__WEBHOOK_SECRET` as the secret token. Details are in [CLAUDE.md](../CLAUDE.md#releases-and-deploys).

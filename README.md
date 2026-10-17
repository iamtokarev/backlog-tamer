# Backlog Tamer

A Telegram bot that turns links and notes into decided Notion backlog items. You send it something you found. It drafts what the thing is, why you saved it, and the next steps. Nothing reaches Notion until you approve.

```
You   https://github.com/google/adk-python  try this for the triage agent

Bot   ADK for Python
      📦 repository · 🔨 build · 🔺 High

      Google's toolkit for building agents as code-first workflows,
      with sessions, tools and human-in-the-loop interrupts.

      🔗 github.com · GitHub
      ☑︎ Run the quickstart agent locally
      ☑︎ Prototype the review step as an interrupt

      [ ✅ Approve & save ]  [ ❌ Reject ]
      [ 🔺 High ▸ ]  [ 🔨 build ▸ ]  [ 📦 repository ▸ ]
      [ 📝 Revise with a note ]
```

Tap **Approve** and the card collapses to `✅ Saved to Notion · 2 tasks`, with **Open in Notion** and **Undo** buttons.

## How it works

1. **Capture.** Send a link, a note, or both.
2. **Draft.** The agent fetches the page and proposes a project with type, intent, priority, a summary and tasks. If the page can't be fetched, the card says so instead of passing off a guess.
3. **Review.** Approve or reject it, tap a chip to change one field, or reply with a note and get a redraft.
4. **Commit.** The approved draft becomes a Project page plus linked Task pages in Notion. A link that's already in your backlog is flagged instead of saved twice.

Why it exists: the problem isn't capturing things, it's deciding about them. See [PRODUCT_SENSE.md](PRODUCT_SENSE.md).

## Run it locally

```sh
cp .env.example .env   # fill in the values below
make run               # polling bot; see the Makefile for webhook, test and lint targets
```

You need a Telegram bot token plus your user ID (only that user is served), an [OpenRouter](https://openrouter.ai) key, a Notion integration token with your Projects and Tasks database IDs, and a `DATABASE_URL`. SQLite is fine locally.

To set up the Notion databases, run `uv run python scripts/migrate_notion_schema.py`. By default it only shows which columns are missing. Add `--apply` to create them. The required and optional columns are listed in [CLAUDE.md](CLAUDE.md#notion-database-schema).

## Deploy

Production runs on AWS Lambda (webhook receiver → SQS → worker), with infrastructure in [`infra/terraform`](infra/terraform). Releases deploy; merges to `main` don't. Merge the standing release-please PR and the tagged version is built, deployed and smoke-tested. To roll back:

```sh
gh workflow run deploy.yml -f version=v0.6.0
```

One-time setup: point the Telegram webhook at the webhook Lambda's Function URL, using `TELEGRAM__WEBHOOK_SECRET` as the secret token. Details are in [CLAUDE.md](CLAUDE.md#releases-and-deploys).

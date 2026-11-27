<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/header-dark.svg">
    <img src="docs/assets/header-light.svg" width="840" alt="backlog-tamer: links in, decided backlog out. You approve every write.">
  </picture>
</p>

<p align="center">
  <a href="#what-it-does">what it does</a> &nbsp;/&nbsp;
  <a href="#review-in-one-tap">review</a> &nbsp;/&nbsp;
  <a href="#how-it-works">how it works</a> &nbsp;/&nbsp;
  <a href="#run-it">run it</a>
</p>

<p align="center">
  <img src="docs/assets/terminal.svg" width="840" alt="Terminal recording of one intake: you send a GitHub link with a note, the bot reads the page, drafts 'ADK for Python' (repository, build, High), waits for review, and after approval saves one project to Notion.">
</p>

<p align="center">
  <a href="https://github.com/iamtokarev/backlog-tamer/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/iamtokarev/backlog-tamer/ci.yml?branch=main&style=flat-square&label=ci&labelColor=21262d" alt="CI status"></a>
  <a href="https://github.com/iamtokarev/backlog-tamer/releases"><img src="https://img.shields.io/github/v/release/iamtokarev/backlog-tamer?style=flat-square&label=release&labelColor=21262d&color=1f6feb" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-1f6feb?style=flat-square&labelColor=21262d" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/triage-decision%20models-8957e5?style=flat-square&labelColor=21262d" alt="Triage by decision models">
</p>

## What it does

<p align="center">
  <img src="docs/assets/features.svg" width="840" alt="Four things it does. Read: understands what you sent, including web pages, GitHub repos, YouTube, X posts and PDFs, rendering dynamic pages in a real browser and saying so when it can't read the source. Draft: decides a type, an intent, a priority and tags from your existing ones, and writes a title. Review: waits until you approve, reject, edit a field, or reply with a note. Commit: writes a Project to Notion, flags duplicates, and offers Undo.">
</p>

## Review in one tap

<p align="center">
  <img src="docs/assets/review.svg" width="840" alt="The draft card offers Approve and save, Reject, and chips for priority and intent. Tapping the priority chip lets you pick High, Medium or Low, and that choice is kept through revisions. After approval the card reads Saved to Notion, with Open in Notion and Undo buttons.">
</p>

Or just reply with a note, like *"this is for the eval project, not general reading"*, and only the fields it is about are decided again.

## How it works

<p align="center">
  <img src="docs/assets/how-it-works.svg" width="840" alt="How it works. System: Telegram sends an update to a webhook Lambda, which checks the secret and user and enqueues it on SQS; a worker Lambda consumes it and runs the intake using Triage (decision models via OpenRouter), the source reader (Browserbase), the review store (Postgres) and Notion. Review loop: Triage drafts, the draft waits for your review, then you approve (commit to Notion), reject, or revise with a note, which decides the fields it is about again.">
</p>

Triage answers typed questions over a fixed set of labels instead of writing free-form drafts, so an intake takes about a second of model time and a revision never touches fields your note didn't mention. Approval is always a button, so free text can never be mistaken for it. Why decision models: [ADR 0001](docs/adr/0001-triage-by-decision-models.md).

## Run it

```sh
git clone https://github.com/iamtokarev/backlog-tamer && cd backlog-tamer
mise install && mise run setup
cp .env.example .env   # telegram, openrouter, browserbase, notion keys
mise run bot
```

Configuration, Notion setup and AWS deploy: [docs/setup.md](docs/setup.md)

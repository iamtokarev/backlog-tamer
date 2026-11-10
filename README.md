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
  <img src="docs/assets/terminal.svg" width="840" alt="Terminal recording of one intake: you send a GitHub link with a note, the bot reads the page, drafts 'ADK for Python' (repository, build, High) with two tasks, waits for review, and after approval saves one project and two tasks to Notion.">
</p>

<p align="center">
  <a href="https://github.com/iamtokarev/backlog-tamer/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/iamtokarev/backlog-tamer/ci.yml?branch=main&style=flat-square&label=ci&labelColor=21262d" alt="CI status"></a>
  <a href="https://github.com/iamtokarev/backlog-tamer/releases"><img src="https://img.shields.io/github/v/release/iamtokarev/backlog-tamer?style=flat-square&label=release&labelColor=21262d&color=1f6feb" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-1f6feb?style=flat-square&labelColor=21262d" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/agent-google--adk-8957e5?style=flat-square&labelColor=21262d" alt="Agent built on Google ADK">
</p>

## What it does

<p align="center">
  <img src="docs/assets/features.svg" width="840" alt="Four things it does. Read: understands what you sent, including web pages, GitHub repos, YouTube, X posts and PDFs, rendering dynamic pages in a real browser and saying so when it can't read the source. Draft: proposes a decision with a type, an intent, a priority and next steps. Review: waits until you approve, reject, edit a field, or reply with a note. Commit: writes a Project with linked Tasks to Notion, flags duplicates, and offers Undo.">
</p>

## Review in one tap

<p align="center">
  <img src="docs/assets/review.svg" width="840" alt="The draft card offers Approve and save, Reject, and chips for priority and intent. Tapping the priority chip lets you pick High, Medium or Low, and that choice is kept through redrafts. After approval the card reads Saved to Notion, 2 tasks, with Open in Notion and Undo buttons.">
</p>

Or just reply with a note, like *"this is for the eval project, not general reading"*, and get a redraft.

## How it works

<p align="center">
  <img src="docs/assets/how-it-works.svg" width="840" alt="How it works. System: Telegram sends an update to a webhook Lambda, which checks the secret and user and enqueues it on SQS; a worker Lambda consumes it and runs the intake using the triage agent (Google ADK), the source reader (Browserbase), the review store (Postgres) and Notion. Review loop: the agent drafts, the workflow pauses for your review, then you approve (commit to Notion), reject, or revise with a note, which sends it back to the draft step.">
</p>

The review step is a Google ADK interrupt, not a chat turn, so free text can never be mistaken for approval.

## Run it

```sh
git clone https://github.com/iamtokarev/backlog-tamer && cd backlog-tamer
cp .env.example .env   # telegram, openrouter, browserbase, notion keys
make run
```

Configuration, Notion setup and AWS deploy: [docs/setup.md](docs/setup.md)

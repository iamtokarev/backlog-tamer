# Backlog Tamer Product Sense

## Product Summary

Backlog Tamer is a Telegram-first personal learning intake system. Its job is to turn messy learning inputs into clear, confirmed decisions before they become backlog debt.

The product sits between curiosity and execution:

- Telegram is the low-friction inbox for links, notes, ideas, courses, docs, repositories, and quick thoughts.
- The agent is the triage layer that interprets the input and proposes what should happen next.
- Notion is the source of truth where confirmed items become structured, retrievable records.

The core promise is simple: when the user sends something to the bot, Backlog Tamer helps decide what it is, why it matters, where it belongs, and what the next action should be.

For web links, the product should not rely only on the raw URL or the user's short note. It should fetch the page when possible, extract useful details, and use that context to understand what the user is sending.

## Problem

The user does not have a capture problem. The user has a decision problem.

Learning resources arrive faster than they are processed. Interesting links, courses, repositories, ideas, and notes accumulate across contexts. Without a triage step, saved items become an undifferentiated backlog where intent is lost, priorities are unclear, and follow-through becomes harder over time.

Backlog Tamer exists to prevent saved learning inputs from becoming vague someday items.

## Product Goal

Build a system that transforms raw learning inputs into confirmed, structured, actionable records.

The product should reduce ambiguity at intake. Every committed item should be more decided than when it arrived.

## Core Loop

The first product loop is:

1. Capture: the user sends a link, note, idea, or resource to the Telegram bot.
2. Interpret: the agent identifies what the item is and infers the user's likely intent.
3. Propose: the bot replies with a structured draft.
4. Confirm: the user approves, adjusts, or rejects the proposal.
5. Commit: the confirmed item is written to Notion.
6. Reuse: the user later reviews and acts from a cleaner learning system.

This loop is the foundation of the product. Other features should wait until this loop feels reliable.

## Product Principles

### Capture should be frictionless

The input should be allowed to be messy. A user might send only a link, a link with a short note, a shorthand instruction, or an unstructured thought. The system should adapt to the input instead of requiring structure upfront.

### Links should be understood, not just stored

When the user sends a web link, the system should fetch the page and inspect available metadata and content. It should use the page title, description, domain, canonical URL, visible content, and any other reliable signals to classify the resource and propose a useful next action.

If the page cannot be fetched, the product should still handle the input gracefully by using the URL, domain, and user note, while making the uncertainty visible in the proposed draft.

### The product should decide, not just store

A saved item without a decision becomes backlog debt. Each item should leave intake with a recommendation: keep as reference, learn next, try this, revisit later, evaluate, or archive.

### Human confirmation is essential

The agent proposes but does not silently commit. The system should show its interpretation and ask the user to confirm or correct it before writing to Notion.

### The product should reduce backlog, not decorate it

The goal is not a prettier database. The goal is fewer vague items, fewer duplicates, fewer unreviewed captures, clearer priorities, and better next actions.

### The product should earn the right to expand

Calendar planning, weekly reviews, study planning, and specialized modules should come later. The product should prove capture, interpretation, confirmation, and structured record creation before expanding into more advanced behavior.

## Core Concepts

### Resource Types

Backlog Tamer should understand these kinds of inputs:

- Article
- Video
- Course
- Documentation
- Repository
- Feature
- Idea
- Reference
- Experiment

### User Intents

The system should infer or ask for intent when needed:

- Save for reference
- Learn next
- Try this
- Revisit later
- Evaluate
- Archive candidate

### Destinations

Confirmed items should be routed into clear Notion destinations:

- Knowledge Base
- Backlog
- This Week
- Courses
- Experiments
- Archived

## What Makes It Feel Smart

Backlog Tamer feels useful when it respects the user's note as a strong signal, resolves ambiguity into a decision, proposes a concrete next action, and asks for confirmation before changing the system of record.

The agent should not behave like a general assistant. It should behave like a triage and decision layer for personal learning inputs.

## Roadmap Sense

The product should grow in this order:

1. Capture and confirm.
2. Structured backlog shaping.
3. Better intent-aware triage.
4. Backlog hygiene and review.
5. Next-best-item guidance.
6. Time and commitment support.
7. Weekly learning operating rhythm.
8. Specialized modules, only after the core loop is trusted.

The near-term focus should stay on Milestone A, followed by Milestone B. If those work well, Backlog Tamer will already be useful.

## Success Criteria

Backlog Tamer is succeeding if:

- Sending something to the bot reduces thinking instead of adding it.
- The proposed draft usually makes sense.
- Confirmation builds trust.
- The Notion backlog becomes cleaner and more actionable over time.
- Fewer items remain in limbo.
- There is a clearer sense each week of what to learn next.

## Product Risk

The main risk is that Backlog Tamer becomes another inbox.

That happens if everything gets stored, nothing gets filtered, next actions stay vague, categories multiply too quickly, or confirmation becomes a rubber stamp.

The product should be judged by whether it reduces backlog ambiguity, not by how much it captures.

## North Star

Backlog Tamer is a triage layer between curiosity and execution.

It captures learning resources and notes, interprets intent, proposes a structured classification, asks for confirmation, and stores the result in Notion so the learning backlog becomes a navigable system instead of an ever-growing pile.

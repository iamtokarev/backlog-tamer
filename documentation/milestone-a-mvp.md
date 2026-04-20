# Milestone A - MVP

## Purpose

Milestone A builds the smallest real Backlog Tamer product loop.

The goal is to prove that a user can send a learning input to Telegram, receive a useful structured proposal, confirm it, and have the confirmed result written to Notion.

## Combined Focus

- Capture and confirm
- Structured backlog shaping

## Core Loop

input -> interpretation -> confirmation -> structured record

This loop is the foundation for everything else. If it does not work reliably, later roadmap work is premature.

## What This Milestone Should Achieve

- Accept learning inputs through Telegram.
- Understand links, notes, ideas, courses, documentation, repositories, and other learning resources.
- Fetch web links when possible and use page details to improve classification.
- Interpret the item and propose a structured draft.
- Ask for confirmation before committing anything.
- Create a structured record in Notion after confirmation.
- Place each item into a clear destination instead of leaving it vague.

## Why It Matters

Milestone A proves that Backlog Tamer can reduce friction at the moment of capture without silently polluting the system of record.

The product becomes useful only if the user can trust the intake loop: send something, review the agent's interpretation, confirm or correct it, and know that Notion receives a clean record.

## High-Level Success Criteria

- Sending items to the bot feels easy.
- The proposed draft usually makes sense.
- Confirmation builds trust.
- The backlog starts to feel cleaner and more intentional.
- Captured web links become understandable records, not raw URLs.

## Out of Scope For This Milestone

- Weekly reviews
- Calendar planning
- Next-best-item recommendations
- Advanced backlog cleanup
- Specialized planner or curator modules

## Implementation Sense

This milestone should prioritize the core integration path over breadth:

1. Telegram receives the input.
2. The system extracts message text, links, and user notes.
3. The system fetches link metadata or page content when possible.
4. The agent proposes title, type, intent, destination, priority, and next action.
5. The user confirms, edits, or rejects the proposal.
6. The confirmed result is written to Notion.

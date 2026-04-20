# Milestone B - Smart Triage

## Purpose

Milestone B makes Backlog Tamer better at understanding intent and reducing backlog rot.

The goal is to move beyond basic intake so the product starts behaving like a decision layer, not a storage bot.

## Combined Focus

- Better decisions from user intent
- Backlog hygiene and review

## What This Milestone Should Achieve

- Treat optional user notes as strong signals.
- Distinguish between reference material, active learning, experiments, and archive candidates.
- Identify stale or low-value items during review flows.
- Reduce ambiguity across the backlog.
- Improve routing accuracy across destinations.
- Make next actions more relevant and concrete.

## Why It Matters

Milestone B is where Backlog Tamer starts reducing the cost of having a backlog.

The product should help decide what matters, what can be archived, what belongs in the knowledge base, and what deserves near-term attention.

## High-Level Success Criteria

- The system routes items more accurately.
- Next actions feel more relevant.
- Fewer items remain in limbo.
- The backlog becomes easier to review and trust.
- The product is visibly reducing ambiguity rather than only organizing it.

## Product Questions To Resolve

- How should the system represent confidence in its proposed classification?
- When should the bot ask a clarifying question instead of proposing a draft?
- What signals make something stale, low-value, or ready to archive?
- How should duplicate or near-duplicate resources be handled?

## Implementation Sense

This milestone should build on the confirmed records from Milestone A. It should use the user's correction history, notes, item status, destination, and review behavior to improve future proposals.

The product should remain conservative: when intent is ambiguous, it should expose uncertainty and invite correction instead of silently committing.

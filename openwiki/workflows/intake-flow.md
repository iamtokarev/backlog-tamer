---
type: Workflow
title: Intake Workflow
description: >-
  End-to-end flow from Telegram message through ADK agent drafting, human
  review interrupt, approval/rejection/revision routing, and Notion commit.
  Includes confirmation lifecycle state machine and request sequence diagrams.
tags: [workflow, adk, lifecycle]
timestamp: 2025-01-20T00:00:00Z
---

# Intake Workflow

The intake workflow is the core product loop. It takes a raw Telegram message, runs it through an AI agent that produces a structured draft, pauses for human review, and either commits the draft to Notion, rejects it, or loops back for revision.

## ADK Workflow Graph

The workflow is defined in `src/backlog_tamer/agents/intake_triage/workflow.py` using Google ADK's `Workflow` class. The graph has three node types and three routes:

```mermaid
flowchart TD
    START([START]) --> DA["draft_agent — LLM produces ProjectDraft"]
    DA --> RHR["request_human_review — emits RequestInput interrupt"]
    RHR --> HHR["handle_human_review — routes on user reply"]
    HHR -->|"approve"| FA["finalize_approval"]
    HHR -->|"reject"| FR["finalize_rejection"]
    HHR -->|"revise"| BRP["build_revision_prompt — adds feedback and loops back"]
    BRP --> DA
    FA --> END1([END])
    FR --> END2([END])
```

*ADK workflow graph: the agent drafts a proposal, pauses for human review, and routes to approval, rejection, or revision with a feedback loop.*

### Workflow Nodes

| Node | File | Behavior |
|------|------|----------|
| `draft_agent` | `agent.py` | LLM agent with `output_schema=ProjectDraft`, `output_key="draft_proposal"`. Can call `fetch_url` tool. |
| `request_human_review` | `workflow.py` | Coerces draft, builds snapshot, emits `RequestInput` interrupt with `interrupt_id="human_review"`. |
| `handle_human_review` | `workflow.py` | Normalizes feedback: "approve" → route `approved`, "reject" → route `rejected`, anything else → route `revise` with feedback in state. |
| `finalize_approval` | `workflow.py` | Returns a terminal string. Actual Notion write happens in `IntakeService.finalize_approval`. |
| `finalize_rejection` | `workflow.py` | Returns a terminal string. |
| `build_revision_prompt` | `prompts.py` | Constructs a revision prompt from latest feedback, current draft snapshot, original intake, and prior review history. Loops back to `draft_agent`. |

### Session State Keys

| Key | Set By | Contains |
|-----|--------|----------|
| `triage_input` | `build_triage_state_delta` | The formatted triage prompt text |
| `draft_proposal` | `draft_agent` (output_key) | The `ProjectDraft` dict |
| `draft_snapshot` | `request_human_review` | Human-readable snapshot of the draft |
| `review_feedback` | `handle_human_review` (on revise) | Latest free-form feedback text |
| `review_history` | `handle_human_review` (on revise) | List of all feedback rounds |
| `fetched_context` | `fetch_url` tool | Dict of URL → `FetchedUrl` result |
| `manual_edits` | `ConfirmationStore.apply_manual_edit` | Fields the user changed via quick-edit buttons |

## End-to-End Request Flow

The `IntakeService` (`src/backlog_tamer/application/intake_service.py`) orchestrates the workflow across multiple interactions:

```mermaid
sequenceDiagram
    participant U as User (Telegram)
    participant H as handlers.py
    participant IS as IntakeService
    participant R as ADK Runner
    participant SS as DatabaseSessionService
    participant CS as ConfirmationStore
    participant NW as NotionWriter

    U->>H: sends message (link/note)
    H->>IS: start_intake(context)
    IS->>SS: create_session()
    IS->>R: run_async(message, state_delta)
    R->>R: draft_agent produces ProjectDraft
    R->>R: request_human_review emits RequestInput
    R-->>IS: events (includes interrupt)
    IS->>SS: get_session() → extract draft
    IS->>CS: create_pending(ConfirmationRecord)
    IS-->>H: IntakeResult(needs_review)
    H-->>U: draft message + Approve/Revise/Reject buttons

    U->>H: taps "Approve" (callback)
    H->>IS: resume_intake(confirmation_id, "approve")
    IS->>CS: get(confirmation_id)
    IS->>R: run_async(review_reply, invocation_id)
    R->>R: handle_human_review routes to "approved"
    R-->>IS: events (route=approved)
    IS->>IS: finalize_approval(confirmation_id)
    IS->>CS: mark_committing_once() → acquired
    IS->>NW: find_project_by_source(source_url)
    alt duplicate found
        IS->>CS: mark_duplicate()
        IS-->>H: IntakeResult(duplicate, notion_url)
        H-->>U: "Already in backlog" + Open existing / Add task
    else no duplicate
        IS->>NW: create_project_with_tasks(draft, context, grounding)
        NW-->>IS: NotionCommitResult
        IS->>CS: mark_committed()
        IS-->>H: IntakeResult(committed, notion_url)
        H-->>U: "Saved" + Open in Notion / Undo
    end
```

### Revision Flow

When the user taps "Revise", `handlers.py` stores the `confirmation_id` in `TelegramStateStore` and prompts the user to send free-text feedback. When the user sends a text message, `handle_message` detects the pending revision and calls `resume_intake` with the feedback text. The workflow routes to `revise`, which builds a revision prompt and loops back to `draft_agent`. The agent re-runs, produces a new `ProjectDraft`, and `request_human_review` emits a new interrupt — starting the review cycle again.

If the user has made any quick-edit button changes before revising, `_with_manual_edits` prepends them to the review reply so the agent preserves those corrections instead of undoing them.

### Quick Edits (No Agent Re-run)

The user can change priority, intent, or resource type directly from the review keyboard without re-running the agent. Tapping a field button (`edit:p:{id}`, `edit:i:{id}`, `edit:t:{id}`) swaps the keyboard for a picker of options. Picking an option calls `ConfirmationStore.apply_manual_edit`, which patches the stored draft in place and records the change in `manual_edits`. The review card re-renders with the updated value.

### Refetch After Failed Page Fetch

When the fetch tool fails, the review card shows a warning and a "Retry fetch" button. Tapping it clears the `fetch_url` cache (so the cached failure is not returned again) and re-runs `resume_intake` with a "fetch the source URL again" instruction, producing a new draft from fresh page content.

### Duplicate Detection

Before creating a new project, `finalize_approval` calls `NotionWriter.find_project_by_source` with the draft's canonical URL (from grounding) or source URL. If an existing project with that URL is found, the confirmation is marked `DUPLICATE` instead of `COMMITTED`, and the user is offered "Open existing" and "Add task there" buttons. Choosing "Add task there" calls `IntakeService.add_to_existing_project`, which attaches the draft's tasks to the existing project page.

### Undo

After a successful commit, the user can tap "Undo" to archive the Notion pages (project + tasks) via `NotionWriter.archive_pages`. The confirmation transitions from `COMMITTED` to `UNDONE`, and the Notion page IDs are cleared from the record.

### Failed Save and Retry

If `create_project_with_tasks` raises, the confirmation is marked `FAILED` with the error string in `failure_reason`. The user sees the full draft and a "Retry save" button. `mark_committing_once` accepts `FAILED` as a valid source state, so retrying does not require re-running the agent — only the Notion write is repeated.

## Confirmation Lifecycle

`ConfirmationRecord` tracks the state of each intake item. The status transitions are enforced by `ConfirmationStore`:

```mermaid
stateDiagram-v2
    [*] --> PENDING_REVIEW: create_pending
    PENDING_REVIEW --> COMMITTING: mark_committing_once (acquired)
    PENDING_REVIEW --> REJECTED: mark_rejected
    PENDING_REVIEW --> PENDING_REVIEW: resume_intake (revise → new draft)
    COMMITTING --> COMMITTED: mark_committed (Notion write success)
    COMMITTING --> DUPLICATE: mark_duplicate (source URL exists)
    COMMITTING --> FAILED: mark_failed (Notion write error)
    COMMITTING --> COMMITTING: mark_committing_once (already committing, not acquired)
    FAILED --> COMMITTING: mark_committing_once (retry)
    COMMITTED --> UNDONE: mark_undone (undo / archive pages)
    DUPLICATE --> COMMITTED: add_to_existing_project (add tasks to existing)
    COMMITTED --> [*]
    REJECTED --> [*]
    UNDONE --> [*]
    DUPLICATE --> [*]
    FAILED --> [*]
```

### Idempotency

`mark_committing_once` is the critical idempotency guard. It atomically transitions a record from `PENDING_REVIEW` or `FAILED` to `COMMITTING` and returns `acquired=True`. If the record is already `COMMITTING` or `COMMITTED`, it returns `acquired=False` — meaning another call is already in progress or has completed. `finalize_approval` checks this before writing to Notion, preventing duplicate writes from duplicate webhook deliveries or concurrent Lambda invocations. The `FAILED` → `COMMITTING` transition enables retry without re-running the agent.

## Dev Workflow Runner

`src/backlog_tamer/dev/run_intake_workflow.py` runs the ADK workflow standalone with `InMemorySessionService`, without Telegram or database persistence. It accepts raw text, notes, and links as CLI arguments, and can optionally auto-resume the review interrupt with a `--review-reply` flag. This is useful for iterating on prompts and schemas without setting up the full stack.

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/agents/intake_triage/workflow.py` | ADK workflow graph, node functions, state keys |
| `src/backlog_tamer/agents/intake_triage/prompts.py` | All prompt templates and builders |
| `src/backlog_tamer/application/intake_service.py` | `start_intake`, `resume_intake`, `finalize_approval`, `undo_commit`, `add_to_existing_project` |
| `src/backlog_tamer/application/confirmation_store.py` | `mark_committing_once` and all status transitions |
| `src/backlog_tamer/application/models.py` | `ConfirmationStatus` enum |
| `src/backlog_tamer/dev/run_intake_workflow.py` | Standable workflow runner for dev |

# Put the ADK workflow behind one seam

Status: resolved

Terms follow `GLOSSARY.md` (Triage turn, Turn outcome, Checkpoint, Quick edit, Revision).

## Why

The intake module (`application/intake_service.py`) drives ADK by hand. It builds the Runner in its constructor, reads stringly-typed session state (`"fetched_context"` is defined in three files), and parses the review interrupt out of raw events. As a result, `start_intake` and `resume_intake` have no tests, grounding is tested by skipping the constructor (`IntakeService.__new__`), and the Telegram handlers reach past the intake module into the store and into `fetch_url`.

There is also a bug that comes from routing on reply text. If the user taps Revise and types "Approve" with no Quick edits pending, the Draft is committed to Notion instead of revised.

## Decisions

1. **The seam sits at the Turn outcome.** A new deep module, `TriageWorkflow` (`agents/intake_triage/triage_workflow.py`), runs one Triage turn and returns a Turn outcome. Two adapters: the ADK graph (DB sessions in prod, in-memory in the dev runner), and a scripted fake in tests.
2. **Turn outcome** is one of three:
   - `NeedsReview(draft, grounding, review_message, checkpoint)`
   - `Approved()`
   - `Rejected(draft)`

   A turn that produces none of the three raises inside the adapter.
3. **Checkpoint** is an opaque value holding user id, session id, invocation id and request-input call id. The intake module stores it and passes it back without reading it. Keep the existing DB columns: no migration.
4. **Moves behind the seam:** grounding extraction, task-name composition (`with_composed_task_names`), the session-state keys (`draft_proposal` and `fetched_context`, each defined once), event and interrupt parsing, building the function response, the lazy `agent.py` import, and the LangSmith setup.
5. **Interface.** Each human decision is a named operation, and replies to the graph are structured (`{"decision": ..., "feedback": ...}`), so free text can never route to approval:
   ```
   start(context, user_id, known_topics) -> TurnOutcome
   revise(checkpoint, feedback, kept_fields) -> TurnOutcome
   refetch(checkpoint) -> TurnOutcome
   approve(checkpoint) -> TurnOutcome
   reject(checkpoint) -> TurnOutcome
   ```
6. **Quick-edit replay.** The intake module passes `kept_fields: dict[str, str]`. The wording that tells the agent to keep those fields moves from `_with_manual_edits` to `prompts.py`.
7. **Refetch** is an operation on both the intake module and TriageWorkflow. The fetch-cache reset and the feedback text move behind the seam. `handlers.py` no longer imports `fetch_url`.
8. **`fetch_url` stops caching error results.**
9. **Construction.** `TriageWorkflow(graph, session_service)`, plus `TriageWorkflow.from_settings(settings, database_url)`. `IntakeService` gains `workflow: TriageWorkflow | None = None`, defaulting to `from_settings`, the same idiom it already uses for store and Notion writer. Entry points stay unchanged.
10. **Dev runner.** `dev/run_intake_workflow.py` uses `TriageWorkflow` with `InMemorySessionService` and drops its own interrupt parsing.
11. **Overlap with candidate #2, part (i).** The intake module gains `get_review(confirmation_id)` and `apply_quick_edit(confirmation_id, field, value)`. `handlers.py` stops touching `intake_service.store`.

## Out of scope

- One transition table for Confirmation statuses, and extending the commit lock to undo and add-to-existing (candidate #2, part ii).
- Tests for the Telegram handlers, and a single callback-data codec (candidate #3).

## Tests

- `tests/test_triage_workflow.py`: real graph via `build_intake_workflow(stub_draft_node)` plus `InMemorySessionService`, with no LLM (spike confirmed this works). Covers start, revise (feedback and `kept_fields` reach the drafting step), "Approve" as revise feedback still needing review, approve, reject, refetch clearing the cache, and the grounding cases moved from `test_draft_grounding.py`.
- `tests/fakes.py`: `ScriptedTriageWorkflow`, which returns queued Turn outcomes and records calls.
- `tests/test_intake_flow.py`: the intake module against the scripted fake. Covers start persisting a pending Confirmation, a revision updating the Draft and clearing Quick edits, Quick edits reaching `revise` as `kept_fields`, approve committing, reject, terminal statuses short-circuiting, `get_review`, and `apply_quick_edit`.
- `test_approval_finalization.py` constructs the intake module with `workflow=ScriptedTriageWorkflow()`.

## Delivery

One PR, three commits, each green:

1. `issues/01`: TriageWorkflow and the intake module switched over
2. `issues/02`: handlers off the store, and refetch as an operation
3. `issues/03`: `fetch_url` stops caching errors

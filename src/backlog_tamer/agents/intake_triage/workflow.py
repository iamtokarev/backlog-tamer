from collections.abc import Callable

from google.adk import Workflow
from google.adk.events import Event, RequestInput
from google.adk.events.event_actions import EventActions
from google.genai import types

from .prompts import (
    build_review_message,
    build_review_snapshot,
    build_revision_prompt,
    build_triage_prompt,
)
from .schemas import DraftProposal, IncomingContext

REVIEW_INTERRUPT_ID = "human_review"
TRIAGE_INPUT_STATE_KEY = "triage_input"
REVIEW_FEEDBACK_STATE_KEY = "review_feedback"
REVIEW_HISTORY_STATE_KEY = "review_history"
FETCHED_CONTEXT_STATE_KEY = "fetched_context"
DRAFT_SNAPSHOT_STATE_KEY = "draft_snapshot"


def build_triage_message(context: IncomingContext) -> types.Content:
    """Build the user message passed into the drafting agent."""
    return types.Content(
        role="user",
        parts=[types.Part.from_text(text=build_triage_prompt(context))],
    )


def build_triage_state_delta(context: IncomingContext) -> dict[str, object]:
    return {
        TRIAGE_INPUT_STATE_KEY: build_triage_prompt(context),
        REVIEW_HISTORY_STATE_KEY: [],
        FETCHED_CONTEXT_STATE_KEY: {},
    }


def request_human_review(
    node_input: DraftProposal | dict[str, object],
    fetched_context: dict[str, object] | None = None,
):
    draft = _coerce_draft_proposal(node_input)
    draft_snapshot = build_review_snapshot(draft, fetched_context)
    yield Event(
        actions=EventActions(
            state_delta={DRAFT_SNAPSHOT_STATE_KEY: draft_snapshot},
        )
    )
    yield RequestInput(
        interrupt_id=REVIEW_INTERRUPT_ID,
        message=build_review_message(draft_snapshot=draft_snapshot),
    )


def handle_human_review(
    node_input: str | dict[str, str],
    review_history: list[str] | None = None,
):
    if isinstance(node_input, dict):
        feedback = node_input.get("value", "")
    else:
        feedback = node_input

    feedback = feedback.strip()
    normalized_feedback = feedback.lower()

    if normalized_feedback == "approve":
        yield Event(route="approved")
        return

    if normalized_feedback == "reject":
        yield Event(route="rejected")
        return

    updated_review_history = [*(review_history or []), feedback]
    yield Event(
        route="revise",
        actions=EventActions(
            state_delta={
                REVIEW_FEEDBACK_STATE_KEY: feedback,
                REVIEW_HISTORY_STATE_KEY: updated_review_history,
            }
        ),
    )


def _coerce_draft_proposal(
    draft_proposal: DraftProposal | dict[str, object],
) -> DraftProposal:
    if isinstance(draft_proposal, DraftProposal):
        return draft_proposal
    return DraftProposal.model_validate(draft_proposal)


def finalize_approval():
    return "Draft approved and ready for persistence."


def finalize_rejection():
    return "Draft rejected. No persistence should happen."


def build_intake_workflow(draft_node: Callable) -> Workflow:
    """Wrap the drafting node in the Phase 1 graph workflow scaffold."""
    return Workflow(
        name="intake_triage_workflow",
        edges=[
            ("START", draft_node, request_human_review, handle_human_review),
            (
                handle_human_review,
                {
                    "approved": finalize_approval,
                    "rejected": finalize_rejection,
                    "revise": build_revision_prompt,
                },
            ),
            (build_revision_prompt, draft_node),
        ],
    )

from collections.abc import Callable
from enum import StrEnum

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
from .schemas import IncomingContext, ProjectDraft

REVIEW_INTERRUPT_ID = "human_review"
TRIAGE_INPUT_STATE_KEY = "triage_input"
REVIEW_FEEDBACK_STATE_KEY = "review_feedback"
REVIEW_HISTORY_STATE_KEY = "review_history"
# Written by the drafting agent (its output_key).
DRAFT_PROPOSAL_STATE_KEY = "draft_proposal"
# Written by fetch_url.
FETCHED_CONTEXT_STATE_KEY = "fetched_context"
DRAFT_SNAPSHOT_STATE_KEY = "draft_snapshot"

REVIEW_DECISION_FIELD = "decision"
REVIEW_FEEDBACK_FIELD = "feedback"

ROUTE_APPROVED = "approved"
ROUTE_REJECTED = "rejected"
ROUTE_REVISE = "revise"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


def build_triage_message(
    context: IncomingContext,
    known_topics: list[str] | None = None,
) -> types.Content:
    """Build the user message passed into the drafting agent."""
    return types.Content(
        role="user",
        parts=[
            types.Part.from_text(text=build_triage_prompt(context, known_topics)),
        ],
    )


def build_triage_state_delta(
    context: IncomingContext,
    known_topics: list[str] | None = None,
) -> dict[str, object]:
    return {
        TRIAGE_INPUT_STATE_KEY: build_triage_prompt(context, known_topics),
        REVIEW_HISTORY_STATE_KEY: [],
        FETCHED_CONTEXT_STATE_KEY: {},
    }


def request_human_review(
    node_input: ProjectDraft | dict[str, object],
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


def build_review_reply(
    decision: ReviewDecision,
    feedback: str = "",
) -> dict[str, str]:
    """The structured answer to the review interrupt.

    Routing on the decision rather than the reply text means revision
    feedback that happens to read "approve" is still a revision.
    """
    return {
        REVIEW_DECISION_FIELD: decision.value,
        REVIEW_FEEDBACK_FIELD: feedback,
    }


def handle_human_review(
    node_input: dict[str, str],
    review_history: list[str] | None = None,
):
    # Raises ValueError for anything that is not a ReviewDecision.
    decision = ReviewDecision(node_input.get(REVIEW_DECISION_FIELD))

    if decision is ReviewDecision.APPROVE:
        yield Event(route=ROUTE_APPROVED)
        return

    if decision is ReviewDecision.REJECT:
        yield Event(route=ROUTE_REJECTED)
        return

    feedback = (node_input.get(REVIEW_FEEDBACK_FIELD) or "").strip()
    updated_review_history = [*(review_history or []), feedback]
    yield Event(
        route=ROUTE_REVISE,
        actions=EventActions(
            state_delta={
                REVIEW_FEEDBACK_STATE_KEY: feedback,
                REVIEW_HISTORY_STATE_KEY: updated_review_history,
            }
        ),
    )


def _coerce_draft_proposal(
    draft_proposal: ProjectDraft | dict[str, object],
) -> ProjectDraft:
    if isinstance(draft_proposal, ProjectDraft):
        return draft_proposal
    return ProjectDraft.model_validate(draft_proposal)


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
                    ROUTE_APPROVED: finalize_approval,
                    ROUTE_REJECTED: finalize_rejection,
                    ROUTE_REVISE: build_revision_prompt,
                },
            ),
            (build_revision_prompt, draft_node),
        ],
    )

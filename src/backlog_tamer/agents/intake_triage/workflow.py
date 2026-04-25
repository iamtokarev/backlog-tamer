from collections.abc import Callable

from google.adk import Workflow
from google.adk.events import Event, RequestInput
from google.genai import types

from .schemas import DraftProposal, IncomingContext

REVIEW_INTERRUPT_ID = "human_review"


def _format_links(context: IncomingContext) -> str:
    if not context.links:
        return "- none"

    return "\n".join(f"- {link.url}" for link in context.links)


def build_triage_prompt(context: IncomingContext) -> str:
    """Render the normalized intake payload into a grounded user prompt."""
    note = context.note or "none"

    return "\n".join(
        [
            "Captured learning item",
            "",
            f"Raw text: {context.raw_text}",
            f"Note: {note}",
            "Links:",
            _format_links(context),
        ]
    )


def build_triage_message(context: IncomingContext) -> types.Content:
    """Build the user message passed into the drafting agent."""
    return types.Content(
        role="user",
        parts=[types.Part.from_text(text=build_triage_prompt(context))],
    )


def _format_draft_for_review(draft: DraftProposal) -> str:
    source_url = draft.source_url or "none"
    return "\n".join(
        [
            "Review the proposed draft:",
            "",
            f"Title: {draft.title}",
            f"Description: {draft.description}",
            f"Resource type: {draft.resource_type}",
            f"Intent: {draft.intent}",
            f"Source URL: {source_url}",
            f"Reasoning: {draft.reasoning}",
            "",
            "Reply with one of:",
            "- approve",
            "- reject",
            "- free-form revision feedback",
        ]
    )


def request_human_review(node_input: DraftProposal):
    yield RequestInput(
        interrupt_id=REVIEW_INTERRUPT_ID,
        message=_format_draft_for_review(node_input),
    )


def handle_human_review(node_input: str | dict[str, str]):
    if isinstance(node_input, dict):
        feedback = (
            node_input.get("value")
            or node_input.get("output")
            or node_input.get("input")
            or ""
        )
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

    revision_prompt = "\n".join(
        [
            "Revise the previous DraftProposal using the user's feedback.",
            "Keep the result grounded in the original input and any tool results.",
            f"User feedback: {feedback}",
        ]
    )
    yield Event(route="revise", output=revision_prompt)


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
                    "revise": draft_node,
                },
            ),
        ],
    )

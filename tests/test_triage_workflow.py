"""TriageWorkflow against the real review graph, with a scripted drafting node.

Only the LLM is replaced: the graph, its interrupt, the structured review
reply and the session state are all the production ones.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk import Workflow
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions import InMemorySessionService

from backlog_tamer.agents.intake_triage.schemas import IncomingContext
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    NeedsReview,
    Rejected,
    TriageWorkflow,
)
from backlog_tamer.agents.intake_triage.workflow import (
    DRAFT_PROPOSAL_STATE_KEY,
    FETCHED_CONTEXT_STATE_KEY,
    build_intake_workflow,
)

SOURCE_URL = "https://blog.langchain.com/langgraph/"


def _draft(**overrides) -> dict:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "short_name": "LangGraph",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "learn",
        "priority": "High",
        "source_url": SOURCE_URL,
        "tasks": ["Read"],
    }
    payload.update(overrides)
    return payload


class StubDraftNode:
    """Stands in for the drafting agent: replays scripted drafts in order.

    Each turn writes the draft (and, when given, the fetch results) to session
    state the way the real agent and `fetch_url` do, and records what the
    graph handed it so tests can see what reached the drafting step.
    """

    def __init__(self, *drafts: dict, fetched: dict | None = None):
        self.drafts = list(drafts) or [_draft()]
        self.fetched = fetched
        self.inputs: list[str] = []

    def as_node(self):
        def draft(node_input):
            self.inputs.append(str(node_input))
            index = min(len(self.inputs), len(self.drafts)) - 1
            payload = self.drafts[index]
            state_delta = {DRAFT_PROPOSAL_STATE_KEY: payload}
            if self.fetched is not None:
                state_delta[FETCHED_CONTEXT_STATE_KEY] = self.fetched
            yield Event(actions=EventActions(state_delta=state_delta))
            yield Event(output=payload)

        return draft


def _workflow(node: StubDraftNode) -> TriageWorkflow:
    return TriageWorkflow(
        build_intake_workflow(node.as_node()),
        InMemorySessionService(),
    )


def _start(workflow: TriageWorkflow, raw_text: str = SOURCE_URL):
    return asyncio.run(
        workflow.start(IncomingContext(raw_text=raw_text), "user-id", []),
    )


def test_start_pauses_for_review_with_the_agents_draft():
    outcome = _start(_workflow(StubDraftNode()))

    assert isinstance(outcome, NeedsReview)
    assert outcome.draft.project_name == (
        "LangGraph: build stateful multi-agent workflows"
    )
    assert "Review the proposed draft" in outcome.review_message


def test_start_names_the_default_task_after_its_subject():
    outcome = _start(_workflow(StubDraftNode(_draft(tasks=["Read"]))))

    assert outcome.draft.tasks == ["Read: LangGraph"]


def test_revise_sends_the_feedback_to_the_drafting_step_and_pauses_again():
    node = StubDraftNode(_draft(), _draft(project_name="LangGraph: stateful agents"))
    workflow = _workflow(node)
    started = _start(workflow)

    outcome = asyncio.run(
        workflow.revise(started.checkpoint, "make the title shorter", {}),
    )

    assert isinstance(outcome, NeedsReview)
    assert outcome.draft.project_name == "LangGraph: stateful agents"
    assert "make the title shorter" in node.inputs[-1]


def test_revise_tells_the_drafting_step_to_keep_quick_edited_fields():
    node = StubDraftNode()
    workflow = _workflow(node)
    started = _start(workflow)

    asyncio.run(
        workflow.revise(
            started.checkpoint,
            "make the title shorter",
            {"priority": "Low", "intent": "reference"},
        ),
    )

    assert "priority=Low" in node.inputs[-1]
    assert "intent=reference" in node.inputs[-1]
    assert "make the title shorter" in node.inputs[-1]


def test_revision_feedback_that_reads_approve_is_still_a_revision():
    node = StubDraftNode()
    workflow = _workflow(node)
    started = _start(workflow)

    outcome = asyncio.run(workflow.revise(started.checkpoint, "Approve", {}))

    assert isinstance(outcome, NeedsReview)
    assert len(node.inputs) == 2


def test_approve_ends_the_triage_without_redrafting():
    node = StubDraftNode()
    workflow = _workflow(node)
    started = _start(workflow)

    outcome = asyncio.run(workflow.approve(started.checkpoint))

    assert outcome == Approved()
    assert len(node.inputs) == 1


def test_reject_ends_the_triage_with_the_last_draft():
    node = StubDraftNode(_draft(), _draft(project_name="LangGraph: stateful agents"))
    workflow = _workflow(node)
    started = _start(workflow)
    revised = asyncio.run(workflow.revise(started.checkpoint, "shorter", {}))

    outcome = asyncio.run(workflow.reject(revised.checkpoint))

    assert isinstance(outcome, Rejected)
    assert outcome.draft.project_name == "LangGraph: stateful agents"


def test_a_turn_without_an_outcome_is_an_error():
    node = StubDraftNode()
    graph = Workflow(name="no_review", edges=[("START", node.as_node())])
    workflow = TriageWorkflow(graph, InMemorySessionService())

    with pytest.raises(RuntimeError):
        _start(workflow)


def _grounding_after_start(draft: dict | None = None, fetched: dict | None = None):
    node = StubDraftNode(draft or _draft(), fetched=fetched)
    return _start(_workflow(node)).grounding


def test_grounding_is_skipped_when_no_url_was_fetched():
    grounding = _grounding_after_start(_draft(source_url=None))

    assert grounding.fetch_status == "skipped"
    assert not grounding.is_degraded


def test_grounding_captures_site_name_and_key_points_on_success():
    grounding = _grounding_after_start(
        fetched={
            SOURCE_URL: {
                "status": "success",
                "title": "LangGraph: Multi-Agent Workflows",
                "site_name": "LangChain Blog",
                "canonical_url": SOURCE_URL,
                "key_points": [
                    "Supervisor pattern",
                    "Hierarchical teams",
                    "",
                    "d",
                    "e",
                ],
            }
        }
    )

    assert grounding.fetch_status == "success"
    assert grounding.site_name == "LangChain Blog"
    assert grounding.page_title == "LangGraph: Multi-Agent Workflows"
    assert grounding.canonical_url == SOURCE_URL
    assert grounding.key_points == [
        "Supervisor pattern",
        "Hierarchical teams",
        "d",
        "e",
    ]


def test_grounding_records_the_failure_when_the_page_could_not_be_read():
    grounding = _grounding_after_start(
        fetched={
            SOURCE_URL: {
                "status": "error",
                "error": "HTTP 403 while fetching URL.",
            }
        }
    )

    assert grounding.is_degraded
    assert grounding.fetch_error == "HTTP 403 while fetching URL."
    assert grounding.key_points == []


def test_grounding_matches_the_entry_for_the_drafts_own_source():
    grounding = _grounding_after_start(
        fetched={
            "https://example.com/other": {"status": "success", "site_name": "Other"},
            SOURCE_URL: {"status": "success", "site_name": "LangChain Blog"},
        }
    )

    assert grounding.site_name == "LangChain Blog"


def test_grounding_matches_the_source_through_a_redirect():
    grounding = _grounding_after_start(
        fetched={
            "https://example.com/other": {"status": "success", "site_name": "Other"},
            "https://lnkd.in/abc": {
                "status": "success",
                "site_name": "LangChain Blog",
                "final_url": SOURCE_URL,
            },
        }
    )

    assert grounding.site_name == "LangChain Blog"

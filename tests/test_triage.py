"""Triage: typed answers in, a Draft out, through classify and revise only."""

from __future__ import annotations

import asyncio
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from backlog_tamer.domain import Grounding, IncomingContext, ProjectDraft, SourceLink
from backlog_tamer.integrations.telegram.rendering import FIELD_OPTIONS
from backlog_tamer.triage import Triage, questions

URL = "https://github.com/langchain-ai/langgraph"
# "learn" is a legacy tag, so the subject vocabulary is langgraph, multi-agent, mcp.
TOPICS = ["learn", "langgraph", "multi-agent", "mcp"]
PAGE = Grounding(
    fetch_status="success",
    site_name="GitHub",
    page_title="langgraph",
    final_url=URL,
    description="Build resilient agents as graphs.",
    key_points=["Stateful graphs"],
)


def choice(label: str, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": label,
        "confidence": confidence,
        "probabilities": {label: confidence},
    }


def noul(p: float) -> dict[str, Any]:
    return {"type": "noul", "noul": p}


def score(level: int) -> dict[str, Any]:
    probabilities = {str(i): (0.8 if i == level else 0.1) for i in range(3)}
    return {
        "type": "score",
        "score": level,
        "confidence": 0.7,
        "probabilities": probabilities,
    }


BASE_ANSWERS = {
    "project_type": choice("repository"),
    "intent": choice("explore", 0.6),
    "priority": score(2),
    "tag_0": noul(0.95),  # langgraph
    "tag_1": noul(0.7),  # multi-agent
    "tag_2": noul(0.1),  # mcp
}
NO_ROUTES = {
    f"change_{field}": noul(0.0)
    for field in ("project_type", "intent", "priority", "project_name", "topics")
}


class FakeClient:
    """Answers every question it is asked from a table; records the calls."""

    def __init__(self, answers: dict[str, Any] | None = None):
        self.answers = {**BASE_ANSWERS, **(answers or {})}
        self.calls: list[tuple[dict[str, Any], list[str]]] = []
        self.title_calls: list[dict[str, Any]] = []

    async def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, list(questions)))
        return {name: self.answers[name] for name in questions if name in self.answers}

    async def title(self, page, *, feedback=None, current=None) -> str:
        self.title_calls.append(
            {"page": page, "feedback": feedback, "current": current}
        )
        if feedback and '"' in feedback:
            return feedback.split('"')[1]
        return "LangGraph: build stateful agents"


def context(note: str | None = None, raw_text: str = URL) -> IncomingContext:
    return IncomingContext(raw_text=raw_text, note=note, links=[SourceLink(url=URL)])


def classify(client: FakeClient, ctx: IncomingContext | None = None, grounding=PAGE):
    return asyncio.run(Triage(client).classify(ctx or context(), grounding, TOPICS))


def revise(client: FakeClient, draft: ProjectDraft, feedback: str) -> ProjectDraft:
    return asyncio.run(Triage(client).revise(context(), PAGE, draft, feedback, TOPICS))


def test_classify_builds_a_draft_from_the_decisions():
    draft = classify(FakeClient())

    assert draft == ProjectDraft(
        project_name="LangGraph: build stateful agents",
        summary="Build resilient agents as graphs.",
        project_type="repository",
        intent="explore",
        priority="Medium",
        source_url=URL,
        topics=["langgraph", "multi-agent"],
    )


def test_type_is_decided_first_and_the_details_see_it():
    client = FakeClient()

    classify(client)

    (first_state, first_qs), (second_state, second_qs) = client.calls
    assert first_qs == ["project_type"]
    assert "decided_project_type" not in first_state
    assert second_state["decided_project_type"] == "repository"
    assert "intent" in second_qs


def test_priority_is_medium_without_a_note():
    client = FakeClient()

    draft = classify(client)

    assert draft.priority == "Medium"
    assert all("priority" not in qs for _, qs in client.calls)


def test_a_note_lets_the_model_set_priority():
    client = FakeClient()

    draft = classify(client, context(note="need this for my bot today"))

    assert draft.priority == "High"
    assert client.calls[1][0]["user_note"] == "need this for my bot today"


def test_revision_changes_only_the_routed_fields():
    client = FakeClient()
    draft = classify(client)
    client.answers.update(
        {
            **NO_ROUTES,
            "change_intent": noul(0.9),
            "intent": choice("build"),
            "project_type": choice("tool"),
        }
    )

    revised = revise(client, draft, "I want to build with it")

    assert revised == draft.model_copy(update={"intent": "build"})
    assert client.calls[-1][0]["reviewer_feedback"] == "I want to build with it"
    assert len(client.title_calls) == 1  # the classify title only


def test_an_exact_title_in_the_feedback_is_used_verbatim():
    client = FakeClient()
    draft = classify(client)
    client.answers.update({**NO_ROUTES, "change_project_name": noul(0.95)})

    revised = revise(client, draft, 'Rename it to "LangGraph"')

    assert revised.project_name == "LangGraph"
    assert client.title_calls[-1]["current"] == draft.project_name
    assert client.title_calls[-1]["feedback"] == 'Rename it to "LangGraph"'


def test_tag_spelling_variants_count_once():
    client = FakeClient({"tag_0": noul(0.9), "tag_1": noul(0.8), "tag_2": noul(0.7)})

    draft = asyncio.run(
        Triage(client).classify(context(), PAGE, ["llm-agents", "LLM agent", "mcp"])
    )

    assert draft.topics == ["llm-agents", "mcp"]


def test_missing_answers_fall_back_to_defaults_on_classify():
    client = FakeClient()
    client.answers = {}

    draft = classify(client)

    assert (draft.project_type, draft.intent, draft.priority) == (
        "product",
        "unclear",
        "Medium",
    )
    assert draft.topics == []


def test_missing_answers_keep_the_current_values_on_revise():
    client = FakeClient()
    draft = classify(client)
    client.answers = {
        f"change_{field}": noul(0.9)
        for field in ("project_type", "intent", "priority", "topics")
    }

    revised = revise(client, draft, "redo it")

    assert revised == draft


def test_a_revision_without_tag_answers_keeps_the_topics():
    client = FakeClient()
    draft = classify(client, context(note="need this for my bot today"))
    assert (draft.priority, draft.topics) == ("High", ["langgraph", "multi-agent"])
    client.answers = {
        **NO_ROUTES,
        "change_priority": noul(0.9),
        "change_topics": noul(0.9),
    }

    revised = revise(client, draft, "lower priority, fix the tags")

    assert (revised.priority, revised.topics) == ("High", ["langgraph", "multi-agent"])


def test_a_type_change_re_asks_intent_with_the_new_type():
    client = FakeClient()
    draft = classify(client)
    client.answers.update(
        {
            **NO_ROUTES,
            "change_project_type": noul(0.9),
            "project_type": choice("course"),
            "intent": choice("learn"),
        }
    )

    revised = revise(client, draft, "this is a course")

    assert (revised.project_type, revised.intent) == ("course", "learn")
    state, asked = client.calls[-1]
    assert asked == ["intent"]
    assert state["decided_project_type"] == "course"
    assert state["reviewer_feedback"] == "this is a course"


def test_an_unchanged_type_keeps_the_revision_to_one_round_trip():
    client = FakeClient()
    draft = classify(client)
    calls_before = len(client.calls)
    client.answers.update(
        {**NO_ROUTES, "change_project_type": noul(0.9), "intent": choice("build")}
    )

    revised = revise(client, draft, "it is a repository, really")

    assert revised == draft
    assert len(client.calls) == calls_before + 1


def test_a_revision_that_does_not_route_the_title_makes_no_title_call():
    client = FakeClient()
    draft = classify(client)
    client.answers.update({**NO_ROUTES, "change_priority": noul(0.9)})

    revise(client, draft, "make it high priority")

    assert len(client.title_calls) == 1  # the classify title only


def test_an_empty_title_from_the_title_call_is_rejected():
    client = FakeClient()
    draft = classify(client)
    client.answers.update({**NO_ROUTES, "change_project_name": noul(0.95)})

    with pytest.raises(ValidationError):
        revise(client, draft, 'Rename it to ""')


def test_every_question_ends_with_the_injection_guard():
    built = {
        "project_type": questions.type_question(),
        "revised_type": questions.type_question(revising=True),
        **questions.detail_questions(["mcp"], has_note=True, revising=True),
        **questions.routing_questions(),
    }

    assert all(q["instructions"].endswith(questions.GUARD) for q in built.values())


def test_a_skipped_grounding_is_judged_from_the_raw_text():
    client = FakeClient()
    note = "Look into hybrid search with pgvector for the bot"
    ctx = IncomingContext(raw_text=note)

    draft = classify(client, ctx, Grounding(fetch_status="skipped"))

    state = client.calls[0][0]
    assert state["text"] == note
    assert "title" not in state
    assert draft.summary == note
    assert draft.source_url is None


def test_an_unreadable_page_is_judged_from_the_url():
    client = FakeClient()

    draft = classify(
        client, grounding=Grounding(fetch_status="error", fetch_error="HTTP 403")
    )

    state = client.calls[0][0]
    assert state["url"] == URL
    assert "could not be fetched" in state["fetch"]
    assert draft.source_url == URL


def test_the_label_set_matches_the_draft_and_the_card():
    fields = ProjectDraft.model_fields

    assert set(get_args(fields["project_type"].annotation)) == set(
        questions.PROJECT_TYPES
    )
    assert set(get_args(fields["intent"].annotation)) == set(questions.INTENTS)
    assert set(FIELD_OPTIONS["project_type"]) == set(questions.PROJECT_TYPES)
    assert set(FIELD_OPTIONS["intent"]) == set(questions.INTENTS)
    assert set(FIELD_OPTIONS["priority"]) == set(questions.PRIORITY_LEVELS)
    assert set(get_args(fields["priority"].annotation)) == set(
        questions.PRIORITY_LEVELS
    )

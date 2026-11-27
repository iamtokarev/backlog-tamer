"""Decision triage prototype: typed answers in, a Draft with no Tasks out."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fakes import FakeNotionWriter, build_settings

from backlog_tamer.agents.decision_triage.client import Title
from backlog_tamer.agents.decision_triage.engine import (
    DecisionSessionStore,
    DecisionTriage,
)
from backlog_tamer.agents.intake_triage.schemas import IncomingContext, SourceLink
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    NeedsReview,
    Rejected,
)
from backlog_tamer.application.intake_service import (
    IntakeService,
    _engine_from_settings,
)
from backlog_tamer.integrations.source_reader import FetchedUrl

URL = "https://github.com/langchain-ai/langgraph"
TOPICS = ["learn", "langgraph", "multi-agent", "mcp"]


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


class FakeClient:
    """Answers every question it is asked from a table; records the calls."""

    def __init__(
        self, answers: dict[str, Any], title: str = "LangGraph: build stateful agents"
    ):
        self.answers = answers
        self.title_text = title
        self.calls: list[tuple[dict, list[str]]] = []
        self.title_calls: list[dict] = []

    async def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, list(questions)))
        return {name: self.answers[name] for name in questions if name in self.answers}

    async def title(self, page, *, feedback=None, current=None) -> Title:
        self.title_calls.append({"feedback": feedback, "current": current})
        name = (
            feedback.split('"')[1] if feedback and '"' in feedback else self.title_text
        )
        return Title(project_name=name, short_name=name.split(":")[0])


class FakeReader:
    def __init__(self):
        self.reads = 0

    async def read(self, url: str) -> FetchedUrl:
        self.reads += 1
        return FetchedUrl(
            status="success",
            requested_url=url,
            final_url=url,
            title="langgraph",
            description="Build resilient agents as graphs.",
            site_name="GitHub",
            key_points=["Stateful graphs"],
        )


# Tag questions are numbered over the subject vocabulary ("learn" is legacy).
BASE_ANSWERS = {
    "project_type": choice("repository"),
    "intent": choice("explore", 0.6),
    "priority": score(2),
    "tag_0": noul(0.95),  # langgraph
    "tag_1": noul(0.7),  # multi-agent
    "tag_2": noul(0.1),  # mcp
}


def engine(tmp_path: Path, answers: dict[str, Any] | None = None):
    client = FakeClient({**BASE_ANSWERS, **(answers or {})})
    reader = FakeReader()
    store = DecisionSessionStore(f"sqlite:///{tmp_path / 'decisions.db'}")
    return DecisionTriage(client, reader, store), client, reader


def context(note: str | None = None) -> IncomingContext:
    return IncomingContext(raw_text=URL, note=note, links=[SourceLink(url=URL)])


def test_start_builds_a_draft_from_decisions_with_no_tasks(tmp_path):
    triage, client, _ = engine(tmp_path)

    outcome = asyncio.run(triage.start(context(), "user", TOPICS))

    assert isinstance(outcome, NeedsReview)
    draft = outcome.draft
    assert draft.project_name == "LangGraph: build stateful agents"
    assert (draft.project_type, draft.intent) == ("repository", "explore")
    assert draft.topics == ["langgraph", "multi-agent"]
    assert draft.tasks == []
    assert draft.summary == "Build resilient agents as graphs."
    assert outcome.grounding.fetch_status == "success"


def test_spelling_variants_of_a_tag_count_once():
    from backlog_tamer.agents.decision_triage.questions import read_topics

    answers = {"tag_0": noul(0.9), "tag_1": noul(0.8), "tag_2": noul(0.7)}

    assert read_topics(answers, ["llm-agents", "llm agents", "mcp"]) == [
        "llm-agents",
        "mcp",
    ]


def test_intent_is_decided_after_type_with_the_type_in_state(tmp_path):
    triage, client, _ = engine(tmp_path)

    asyncio.run(triage.start(context(), "user", TOPICS))

    (first_state, first_qs), (second_state, second_qs) = client.calls
    assert first_qs == ["project_type"]
    assert "decided_project_type" not in first_state
    assert second_state["decided_project_type"] == "repository"
    assert "intent" in second_qs


def test_priority_is_medium_without_a_note(tmp_path):
    triage, client, _ = engine(tmp_path)

    outcome = asyncio.run(triage.start(context(), "user", TOPICS))

    assert outcome.draft.priority == "Medium"
    assert all("priority" not in qs for _, qs in client.calls)


def test_a_note_lets_the_model_set_priority(tmp_path):
    triage, client, _ = engine(tmp_path)

    outcome = asyncio.run(
        triage.start(context(note="need this for my bot today"), "user", TOPICS)
    )

    assert outcome.draft.priority == "High"
    assert client.calls[1][0]["user_note"] == "need this for my bot today"


def test_revision_changes_only_routed_fields(tmp_path):
    triage, client, _ = engine(tmp_path)
    started = asyncio.run(triage.start(context(), "user", TOPICS))
    client.answers.update(
        {
            "change_project_type": noul(0.1),
            "change_intent": noul(0.9),
            "change_priority": noul(0.2),
            "change_project_name": noul(0.1),
            "change_topics": noul(0.1),
            "intent": choice("build"),
            "project_type": choice("tool"),
        }
    )

    outcome = asyncio.run(
        triage.revise(started.checkpoint, "I want to build with it", {})
    )

    assert outcome.draft.intent == "build"
    assert outcome.draft.project_type == "repository"  # not routed, kept
    assert outcome.draft.project_name == started.draft.project_name
    state = client.calls[-1][0]
    assert state["reviewer_feedback"] == "I want to build with it"
    assert client.title_calls[-1]["feedback"] is None  # no title re-run


def test_revision_reruns_the_title_only_when_routed(tmp_path):
    triage, client, _ = engine(tmp_path)
    started = asyncio.run(triage.start(context(), "user", TOPICS))
    client.answers.update(
        {
            f"change_{f}": noul(0.0)
            for f in ("project_type", "intent", "priority", "topics")
        }
    )
    client.answers["change_project_name"] = noul(0.95)

    outcome = asyncio.run(
        triage.revise(started.checkpoint, 'Rename it to "LangGraph"', {})
    )

    assert outcome.draft.project_name == "LangGraph"
    assert client.title_calls[-1]["current"] == started.draft.project_name


def test_quick_edits_survive_a_revision_that_routes_their_field(tmp_path):
    triage, client, _ = engine(tmp_path)
    started = asyncio.run(triage.start(context(), "user", TOPICS))
    client.answers.update(
        {
            f"change_{f}": noul(0.9)
            for f in ("project_type", "intent", "priority", "project_name", "topics")
        }
    )
    client.answers["project_type"] = choice("paper")

    outcome = asyncio.run(
        triage.revise(started.checkpoint, "redo it", {"project_type": "course"})
    )

    assert outcome.draft.project_type == "course"


def test_refetch_reads_the_page_again(tmp_path):
    triage, _, reader = engine(tmp_path)
    started = asyncio.run(triage.start(context(), "user", TOPICS))

    asyncio.run(triage.refetch(started.checkpoint, {}))

    assert reader.reads == 2


def test_approve_and_reject_end_the_review(tmp_path):
    triage, _, _ = engine(tmp_path)
    started = asyncio.run(triage.start(context(), "user", TOPICS))

    assert isinstance(asyncio.run(triage.approve(started.checkpoint)), Approved)
    rejected = asyncio.run(triage.reject(started.checkpoint))
    assert isinstance(rejected, Rejected)
    assert rejected.draft == started.draft


def test_settings_select_the_decision_engine(tmp_path):
    settings = build_settings(f"sqlite:///{tmp_path / 'app.db'}")
    settings.triage.engine = "decision"

    assert isinstance(
        _engine_from_settings(settings, settings.database_url), DecisionTriage
    )


def test_approval_commits_a_project_with_no_tasks(tmp_path):
    triage, _, _ = engine(tmp_path)
    writer = FakeNotionWriter()
    service = IntakeService(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        settings=build_settings(),
        notion_writer=writer,
        workflow=triage,
    )
    started = asyncio.run(service.start_intake(context(), "user"))

    result = asyncio.run(service.approve(started.confirmation_id))

    assert result.status == "committed"
    assert result.draft_proposal.tasks == []

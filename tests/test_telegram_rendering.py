from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus
from backlog_tamer.integrations.telegram.rendering import (
    render_draft_message,
    render_terminal_message,
)


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes with explicit control flow.",
        "resource_type": "documentation",
        "intent": "build",
        "priority": "High",
        "source_url": "https://blog.langchain.com/langgraph-multi-agent-workflows/",
        "tasks": ["Explore"],
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def test_draft_message_renders_every_field():
    message = render_draft_message(_draft())

    assert "LangGraph: build stateful multi-agent workflows" in message
    assert "documentation" in message
    assert "build" in message
    assert "High" in message
    assert "Explore" in message
    assert "blog.langchain.com" in message


def test_draft_message_escapes_html_in_model_output():
    message = render_draft_message(
        _draft(
            project_name="Compare <script> & sandboxing",
            summary="Why <iframe> sandboxing matters",
        )
    )

    assert "<script>" not in message
    assert "&lt;script&gt;" in message
    assert "&lt;iframe&gt;" in message
    assert "&amp;" in message


def test_draft_message_survives_markdown_characters_in_titles():
    message = render_draft_message(
        _draft(project_name="pytest_asyncio *and* [markers]"),
    )

    assert "pytest_asyncio *and* [markers]" in message


def test_draft_message_handles_a_draft_without_tasks_or_source():
    message = render_draft_message(_draft(tasks=[], source_url=None))

    assert "Source" not in message
    assert message.endswith("<b>Tasks:</b>\n")


def test_terminal_message_shows_status_badge_and_notion_link():
    message = render_terminal_message(
        _draft(),
        ConfirmationStatus.COMMITTED,
        "https://notion.so/project-id",
    )

    assert "✅ <b>Saved</b>" in message
    assert 'href="https://notion.so/project-id"' in message

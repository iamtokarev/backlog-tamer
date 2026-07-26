from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import (
    IncomingContext,
    ProjectDraft,
    SourceLink,
)
from backlog_tamer.application.models import ConfirmationStatus
from backlog_tamer.integrations.telegram.rendering import (
    render_draft_message,
    render_progress_message,
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


def test_draft_message_leads_with_the_title_then_a_single_chip_line():
    message = render_draft_message(_draft())
    first, second = message.split("\n")[:2]

    assert first == "<b>LangGraph: build stateful multi-agent workflows</b>"
    assert second == "📘 documentation · 🔨 build · 🔺 High"


def test_draft_message_links_the_domain_not_the_raw_url():
    message = render_draft_message(_draft())

    assert (
        '🔗 <a href="https://blog.langchain.com/langgraph-multi-agent-workflows/">'
        "blog.langchain.com</a>" in message
    )


def test_draft_message_strips_www_from_the_displayed_domain():
    message = render_draft_message(_draft(source_url="https://www.arxiv.org/abs/1"))

    assert ">arxiv.org</a>" in message


def test_draft_message_handles_a_draft_without_tasks_or_source():
    message = render_draft_message(_draft(tasks=[], source_url=None))

    assert "🔗" not in message
    assert "☑︎" not in message
    assert message.endswith("Graph of stateful nodes with explicit control flow.")


def test_progress_message_names_the_page_being_read():
    incoming = IncomingContext(
        raw_text="https://blog.langchain.com/langgraph-multi-agent-workflows/",
        links=[
            SourceLink(
                url="https://blog.langchain.com/langgraph-multi-agent-workflows/"
            )
        ],
    )

    assert render_progress_message(incoming) == "🔎 Reading blog.langchain.com…"


def test_progress_message_falls_back_when_there_is_no_link():
    incoming = IncomingContext(raw_text="idea: try duckdb for the eval store")

    assert render_progress_message(incoming) == "🧠 Triaging your note…"


def test_terminal_message_shows_status_badge_and_notion_link():
    message = render_terminal_message(
        _draft(),
        ConfirmationStatus.COMMITTED,
        "https://notion.so/project-id",
    )

    assert "✅ <b>Saved</b>" in message
    assert 'href="https://notion.so/project-id"' in message

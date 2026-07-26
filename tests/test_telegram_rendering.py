from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import (
    IncomingContext,
    ProjectDraft,
    SourceLink,
)
from backlog_tamer.application.models import ConfirmationStatus
from backlog_tamer.integrations.telegram.rendering import (
    build_terminal_keyboard,
    render_change_summary,
    render_draft_message,
    render_progress_message,
    render_revision_prompt,
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


def test_change_summary_names_moved_fields():
    before = _draft()
    after = _draft(priority="Medium", tasks=["Read", "Prototype"])

    summary = render_change_summary(before, after)

    assert summary is not None
    assert "priority High → Medium" in summary
    assert "tasks 1 → 2" in summary


def test_change_summary_reports_rewritten_prose_without_quoting_it():
    before = _draft()
    after = _draft(summary="A much shorter summary.")

    summary = render_change_summary(before, after)

    assert summary is not None
    assert "summary rewritten" in summary
    assert "A much shorter summary." not in summary


def test_change_summary_is_absent_when_nothing_moved():
    assert render_change_summary(_draft(), _draft()) is None


def test_revision_prompt_names_the_draft_being_revised():
    prompt = render_revision_prompt(_draft())

    assert "LangGraph: build stateful multi-agent workflows" in prompt
    assert "Cancel" in prompt


def test_committed_message_is_compact_and_links_out_via_a_button():
    message = render_terminal_message(
        _draft(),
        ConfirmationStatus.COMMITTED,
        "https://notion.so/project-id",
    )
    keyboard = build_terminal_keyboard(
        ConfirmationStatus.COMMITTED,
        "confirmation-id",
        "https://notion.so/project-id",
    )

    assert "✅ <b>Saved to Notion</b>" in message
    assert "LangGraph: build stateful multi-agent workflows" in message
    assert "1 task" in message
    # The whole draft is no longer repeated once it lives in Notion.
    assert "Graph of stateful nodes" not in message
    assert "notion.so" not in message

    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert buttons[0].url == "https://notion.so/project-id"
    assert buttons[1].callback_data == "undo:confirmation-id"


def test_duplicate_message_says_when_it_was_first_saved():
    message = render_terminal_message(
        _draft(),
        ConfirmationStatus.DUPLICATE,
        "https://notion.so/existing",
        duplicate_created_time="2026-06-04T09:12:00.000Z",
    )
    keyboard = build_terminal_keyboard(
        ConfirmationStatus.DUPLICATE,
        "confirmation-id",
        "https://notion.so/existing",
    )
    actions = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "🔁 <b>Already in your backlog</b>" in message
    assert "since 04 Jun 2026" in message
    assert actions == ["addtask:confirmation-id"]


def test_failed_message_keeps_the_whole_draft_and_a_retry_button():
    message = render_terminal_message(
        _draft(),
        ConfirmationStatus.FAILED,
        failure_reason="Server error '502 Bad Gateway'",
    )
    keyboard = build_terminal_keyboard(ConfirmationStatus.FAILED, "confirmation-id")

    assert "502 Bad Gateway" in message
    assert "Graph of stateful nodes" in message
    assert keyboard.inline_keyboard[0][0].callback_data == "retry:confirmation-id"


def test_undone_message_tells_the_user_how_to_get_it_back():
    message = render_terminal_message(_draft(), ConfirmationStatus.UNDONE)

    assert "↩️ <b>Removed from Notion</b>" in message
    assert "Send the item again" in message
    assert build_terminal_keyboard(ConfirmationStatus.UNDONE, "confirmation-id") is None

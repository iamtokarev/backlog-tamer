from __future__ import annotations

from backlog_tamer.domain import ProjectDraft
from backlog_tamer.integrations.notion.writer import build_project_children
from backlog_tamer.integrations.source_reader import FetchedUrl
from backlog_tamer.integrations.telegram.rendering import (
    build_review_keyboard,
    render_draft_message,
)

URL = "https://blog.langchain.com/langgraph/"


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "build",
        "priority": "High",
        "source_url": URL,
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def _actions(keyboard) -> list[str]:
    return [
        button.callback_data.split(":")[0]
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_a_failed_read_is_visible_on_the_card_and_offers_a_retry():
    grounding = FetchedUrl(
        status="error",
        requested_url=URL,
        error="HTTP 403 while fetching URL.",
        title="ignored on error",
    ).to_grounding()

    card = render_draft_message(_draft(), grounding)
    actions = _actions(build_review_keyboard(_draft(), "confirmation-id", grounding))

    assert grounding.is_degraded
    assert grounding.page_title is None
    assert "Couldn't open the page" in card
    assert "HTTP 403" in card
    assert "refetch" in actions


def test_a_successful_read_shows_the_site_name_and_no_warning():
    grounding = FetchedUrl(
        status="success", requested_url=URL, site_name="LangChain Blog"
    ).to_grounding()

    card = render_draft_message(_draft(), grounding)
    actions = _actions(build_review_keyboard(_draft(), "confirmation-id", grounding))

    assert "LangChain Blog" in card
    assert "Couldn't open" not in card
    assert "refetch" not in actions


def test_a_successful_read_keeps_the_page_for_later_turns():
    grounding = FetchedUrl(
        status="success",
        requested_url="https://bit.ly/lg",
        final_url=URL,
        canonical_url="https://www.langchain.com/langgraph",
        domain="blog.langchain.com",
        page_kind="html",
        title="LangGraph",
        description="Build resilient agents as graphs.",
        author="LangChain",
        published_at="2026-01-02",
        content_preview="LangGraph is a low-level orchestration framework.",
    ).to_grounding()

    assert grounding.model_dump(exclude={"key_points"}) == {
        "fetch_status": "success",
        "fetch_error": None,
        "site_name": None,
        "page_title": "LangGraph",
        "canonical_url": "https://www.langchain.com/langgraph",
        "final_url": URL,
        "description": "Build resilient agents as graphs.",
        "author": "LangChain",
        "published_at": "2026-01-02",
        "domain": "blog.langchain.com",
        "page_kind": "html",
        "content_preview": "LangGraph is a low-level orchestration framework.",
    }


def test_at_most_four_key_points_reach_the_notion_page_body():
    grounding = FetchedUrl(
        status="success",
        requested_url=URL,
        site_name="LangChain Blog",
        key_points=["One", "Two", "Three", "Four", "Five"],
    ).to_grounding()

    children = build_project_children(_draft(), grounding=grounding)
    bullets = [
        "".join(
            fragment["text"]["content"]
            for fragment in block["bulleted_list_item"]["rich_text"]
        )
        for block in children
        if block["type"] == "bulleted_list_item"
    ]
    callout = next(block for block in children if block["type"] == "callout")
    callout_text = "".join(
        fragment["text"]["content"] for fragment in callout["callout"]["rich_text"]
    )

    assert bullets == ["One", "Two", "Three", "Four"]
    assert "LangChain Blog" in callout_text

from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import DraftGrounding, ProjectDraft
from backlog_tamer.integrations.notion.writer import build_project_children
from backlog_tamer.integrations.telegram.rendering import (
    build_review_keyboard,
    render_draft_message,
)


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "build",
        "priority": "High",
        "source_url": "https://blog.langchain.com/langgraph/",
        "tasks": ["Explore"],
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def test_degraded_fetch_is_visible_on_the_card_and_offers_a_retry():
    grounding = DraftGrounding(
        fetch_status="error",
        fetch_error="HTTP 403 while fetching URL.",
    )

    card = render_draft_message(_draft(), grounding)
    keyboard = build_review_keyboard(_draft(), "confirmation-id", grounding)
    actions = [
        button.callback_data.split(":")[0]
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "Couldn't open the page" in card
    assert "HTTP 403" in card
    assert "refetch" in actions


def test_successful_fetch_shows_the_site_name_and_no_warning():
    grounding = DraftGrounding(fetch_status="success", site_name="LangChain Blog")

    card = render_draft_message(_draft(), grounding)
    keyboard = build_review_keyboard(_draft(), "confirmation-id", grounding)
    actions = [
        button.callback_data.split(":")[0]
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "LangChain Blog" in card
    assert "Couldn't open" not in card
    assert "refetch" not in actions


def test_key_points_reach_the_notion_page_body():
    grounding = DraftGrounding(
        fetch_status="success",
        site_name="LangChain Blog",
        key_points=["Supervisor pattern routes work", "Hierarchical teams nest graphs"],
    )

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

    assert bullets == [
        "Supervisor pattern routes work",
        "Hierarchical teams nest graphs",
    ]
    assert "LangChain Blog" in callout_text

from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import DraftGrounding, ProjectDraft
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.integrations.notion.writer import build_project_children
from backlog_tamer.integrations.telegram.rendering import (
    build_review_keyboard,
    render_draft_message,
)


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "resource_type": "documentation",
        "intent": "build",
        "priority": "High",
        "source_url": "https://blog.langchain.com/langgraph/",
        "tasks": ["Explore"],
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def _extract(state: dict, draft: ProjectDraft) -> DraftGrounding:
    service = IntakeService.__new__(IntakeService)
    return service._extract_grounding(state, draft)


def test_grounding_is_skipped_when_no_url_was_fetched():
    grounding = _extract({}, _draft(source_url=None))

    assert grounding.fetch_status == "skipped"
    assert not grounding.is_degraded


def test_grounding_captures_site_name_and_key_points_on_success():
    state = {
        "fetched_context": {
            "https://blog.langchain.com/langgraph/": {
                "status": "success",
                "title": "LangGraph: Multi-Agent Workflows",
                "site_name": "LangChain Blog",
                "canonical_url": "https://blog.langchain.com/langgraph/",
                "key_points": [
                    "Supervisor pattern",
                    "Hierarchical teams",
                    "",
                    "d",
                    "e",
                ],
            }
        }
    }

    grounding = _extract(state, _draft())

    assert grounding.fetch_status == "success"
    assert grounding.site_name == "LangChain Blog"
    assert grounding.page_title == "LangGraph: Multi-Agent Workflows"
    assert grounding.key_points == [
        "Supervisor pattern",
        "Hierarchical teams",
        "d",
        "e",
    ]


def test_grounding_records_the_failure_when_the_page_could_not_be_read():
    state = {
        "fetched_context": {
            "https://blog.langchain.com/langgraph/": {
                "status": "error",
                "error": "HTTP 403 while fetching URL.",
            }
        }
    }

    grounding = _extract(state, _draft())

    assert grounding.is_degraded
    assert grounding.fetch_error == "HTTP 403 while fetching URL."
    assert grounding.key_points == []


def test_grounding_matches_the_entry_for_the_drafts_own_source():
    state = {
        "fetched_context": {
            "https://example.com/other": {"status": "success", "site_name": "Other"},
            "https://blog.langchain.com/langgraph/": {
                "status": "success",
                "site_name": "LangChain Blog",
            },
        }
    }

    assert _extract(state, _draft()).site_name == "LangChain Blog"


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

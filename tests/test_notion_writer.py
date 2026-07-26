from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from backlog_tamer.agents.intake_triage.schemas import (
    IncomingContext,
    ProjectDraft,
    SourceLink,
)
from backlog_tamer.integrations.notion.writer import NotionApiError, NotionWriter


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


ALL_PROJECT_PROPERTIES = {
    "Project name",
    "Status",
    "Priority",
    "Type",
    "Intent",
    "Source",
    "Tags",
    "Captured",
    "Summary",
}
ALL_TASK_PROPERTIES = {
    "Task name",
    "Status",
    "Priority",
    "Projects",
    "Due",
    "Source",
}


class FakeClient:
    """Records what the writer sends, and how many requests overlapped."""

    def __init__(
        self,
        project_properties: set[str] | None = None,
        task_properties: set[str] | None = None,
    ):
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.project_properties = (
            ALL_PROJECT_PROPERTIES if project_properties is None else project_properties
        )
        self.task_properties = (
            ALL_TASK_PROPERTIES if task_properties is None else task_properties
        )
        self.open_requests = 0
        self.max_concurrent_requests = 0

    async def get(self, url: str, *, headers) -> FakeResponse:
        self.gets.append(url)
        names = (
            self.project_properties if "projects-db" in url else self.task_properties
        )
        return FakeResponse({"properties": {name: {} for name in names}})

    async def post(self, url: str, *, headers, json) -> FakeResponse:
        self.posts.append((url, json))
        index = len(self.posts)
        self.open_requests += 1
        self.max_concurrent_requests = max(
            self.max_concurrent_requests,
            self.open_requests,
        )
        await asyncio.sleep(0)
        self.open_requests -= 1
        return FakeResponse(
            {"id": f"page-{index}", "url": f"https://notion.so/page-{index}"}
        )


def test_builds_project_payload_with_summary_source_and_fixed_properties():
    draft = ProjectDraft(
        project_name="Learn ADK callbacks",
        summary="Understand how callbacks fit the approval workflow.",
        resource_type="documentation",
        intent="learn",
        priority="Medium",
        source_url="https://example.com/adk",
        tasks=["Read docs", "Sketch integration"],
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_project_payload(draft, captured_at=date(2026, 7, 26))

    assert payload["parent"] == {"database_id": "projects-db"}
    # Notion refuses a page that carries both a template and children.
    assert "template" not in payload
    properties = payload["properties"]
    assert (
        properties["Project name"]["title"][0]["text"]["content"]
        == "Learn ADK callbacks"
    )
    assert properties["Status"] == {"status": {"name": "Backlog"}}
    assert properties["Priority"] == {"select": {"name": "Medium"}}
    assert properties["Type"] == {"select": {"name": "documentation"}}
    assert properties["Intent"] == {"select": {"name": "learn"}}
    assert properties["Source"] == {"url": "https://example.com/adk"}
    assert properties["Captured"] == {"date": {"start": "2026-07-26"}}
    assert properties["Tags"] == {
        "multi_select": [{"name": "documentation"}, {"name": "learn"}]
    }


def test_summary_property_carries_only_the_summary():
    draft = ProjectDraft(
        project_name="Learn ADK callbacks",
        summary="Understand how callbacks fit the approval workflow.",
        resource_type="documentation",
        intent="learn",
        priority="Medium",
        source_url="https://example.com/adk",
        tasks=["Read docs"],
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    summary = writer.build_project_payload(draft)["properties"]["Summary"]

    assert (
        summary["rich_text"][0]["text"]["content"]
        == "Understand how callbacks fit the approval workflow."
    )


def test_project_payload_omits_source_when_the_draft_has_none():
    draft = ProjectDraft(
        project_name="Idea: batch the eval runs",
        summary="Batch nightly evals instead of per-commit.",
        resource_type="idea",
        intent="build",
        priority="Low",
        tasks=["Sketch"],
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    assert "Source" not in writer.build_project_payload(draft)["properties"]


def test_builds_project_payload_normalizes_low_signal_tags():
    draft = ProjectDraft(
        project_name="Mystery item",
        summary="Figure out what this is.",
        resource_type="unknown",
        intent="unclear",
        priority="Low",
        tasks=["Review item"],
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_project_payload(draft)

    assert payload["properties"]["Tags"] == {"multi_select": [{"name": "explore"}]}
    assert payload["icon"] == {"type": "emoji", "emoji": "❔"}


def test_project_page_icon_follows_the_resource_type():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    icons = {
        resource_type: writer.build_project_payload(
            ProjectDraft(
                project_name="Example",
                summary="Example summary.",
                resource_type=resource_type,
                intent="learn",
                priority="Low",
                tasks=["Read"],
            )
        )["icon"]["emoji"]
        for resource_type in ("article", "video", "repository", "documentation")
    }

    assert icons == {
        "article": "📄",
        "video": "🎬",
        "repository": "📦",
        "documentation": "📘",
    }


def test_builds_task_payload_with_project_relation():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_task_payload(
        task_name="Read docs",
        priority="High",
        project_id="project-page-id",
    )

    assert payload["parent"] == {"database_id": "tasks-db"}
    assert payload["template"] == {"type": "default"}
    properties = payload["properties"]
    assert properties["Task name"]["title"][0]["text"]["content"] == "Read docs"
    assert properties["Status"] == {"status": {"name": "Not started"}}
    assert properties["Priority"] == {"select": {"name": "High"}}
    assert properties["Projects"] == {"relation": [{"id": "project-page-id"}]}


def test_commit_reuses_one_client_and_writes_tasks_concurrently():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="High",
        tasks=["Read", "Summarise", "Apply"],
    )

    result = asyncio.run(writer.create_project_with_tasks(draft))

    assert result.project_id == "page-1"
    assert result.task_ids == ["page-2", "page-3", "page-4"]
    assert client.max_concurrent_requests == 3
    assert all(
        payload["parent"] == {"database_id": "tasks-db"}
        for _, payload in client.posts[1:]
    )


def test_tasks_relate_back_to_the_created_project():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="High",
        tasks=["Read"],
    )

    asyncio.run(writer.create_project_with_tasks(draft))

    _, task_payload = client.posts[1]
    assert task_payload["properties"]["Projects"] == {"relation": [{"id": "page-1"}]}


def test_commit_skips_properties_the_database_does_not_have():
    client = FakeClient(project_properties={"Project name", "Status", "Summary"})
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="High",
        source_url="https://example.com",
        tasks=["Read"],
    )

    asyncio.run(writer.create_project_with_tasks(draft))

    _, project_payload = client.posts[0]
    assert set(project_payload["properties"]) == {"Project name", "Status", "Summary"}
    assert project_payload["icon"] == {"type": "emoji", "emoji": "📄"}


def test_commit_reads_the_schema_once_per_writer():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="High",
        tasks=["Read"],
    )

    asyncio.run(writer.create_project_with_tasks(draft))
    asyncio.run(writer.create_project_with_tasks(draft))

    assert len(client.gets) == 2
    assert len(set(client.gets)) == 2


def test_schema_report_is_healthy_when_every_property_exists():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )

    report = asyncio.run(writer.describe_schema())

    assert report.is_healthy
    assert report.missing_project_properties == []
    assert report.missing_task_properties == []
    assert report.skipped_project_properties == []


def test_schema_report_separates_renamed_columns_from_not_yet_added_ones():
    client = FakeClient(
        project_properties={"Status", "Priority", "Tags", "Summary"},
        task_properties={"Task name", "Status", "Priority"},
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )

    report = asyncio.run(writer.describe_schema())

    assert not report.is_healthy
    assert report.missing_project_properties == ["Project name"]
    assert report.missing_task_properties == ["Projects"]
    assert report.skipped_project_properties == [
        "Captured",
        "Intent",
        "Source",
        "Type",
    ]


def _children_of(payload: dict[str, Any], block_type: str) -> list[dict[str, Any]]:
    return [block for block in payload["children"] if block["type"] == block_type]


def _plain_text(block: dict[str, Any]) -> str:
    return "".join(
        fragment["text"]["content"] for fragment in block[block["type"]]["rich_text"]
    )


def test_page_body_bookmarks_the_source_and_keeps_the_original_note():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="LangGraph: build stateful multi-agent workflows",
        summary="Graph of stateful nodes.",
        resource_type="documentation",
        intent="build",
        priority="High",
        source_url="https://blog.langchain.com/langgraph/",
        tasks=["Explore"],
    )
    context = IncomingContext(
        raw_text="https://blog.langchain.com/langgraph/ want to try this for intake",
        note="want to try this for intake",
        links=[SourceLink(url="https://blog.langchain.com/langgraph/")],
    )

    payload = writer.build_project_payload(
        draft,
        captured_at=date(2026, 7, 26),
        incoming_context=context,
    )

    bookmarks = _children_of(payload, "bookmark")
    assert bookmarks[0]["bookmark"]["url"] == "https://blog.langchain.com/langgraph/"

    paragraphs = [_plain_text(block) for block in _children_of(payload, "paragraph")]
    assert "want to try this for intake" in paragraphs

    todos = [_plain_text(block) for block in _children_of(payload, "to_do")]
    assert todos == ["Explore"]

    callout = _plain_text(_children_of(payload, "callout")[0])
    assert "2026-07-26" in callout


def test_page_body_skips_the_note_when_the_message_was_only_a_link():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="Low",
        source_url="https://example.com",
        tasks=["Read"],
    )
    context = IncomingContext(
        raw_text="https://example.com",
        links=[SourceLink(url="https://example.com")],
    )

    payload = writer.build_project_payload(draft, incoming_context=context)

    assert _children_of(payload, "paragraph") == []
    assert "Why I saved this" not in [
        _plain_text(block) for block in _children_of(payload, "heading_3")
    ]


def test_page_body_splits_text_over_the_notion_fragment_limit():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="idea",
        intent="build",
        priority="Low",
        tasks=["Sketch"],
    )
    context = IncomingContext(raw_text="x" * 4500, note="x" * 4500)

    payload = writer.build_project_payload(draft, incoming_context=context)

    paragraph = _children_of(payload, "paragraph")[0]
    fragments = paragraph["paragraph"]["rich_text"]
    assert len(fragments) == 3
    assert all(len(f["text"]["content"]) <= 2000 for f in fragments)
    assert _plain_text(paragraph) == "x" * 4500


def test_tags_carry_topics_when_the_draft_has_them():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="LangGraph: build stateful multi-agent workflows",
        summary="Graph of stateful nodes.",
        resource_type="documentation",
        intent="build",
        priority="High",
        topics=["LangGraph", "Multi-Agent", "orchestration"],
        tasks=["Explore"],
    )

    payload = writer.build_project_payload(draft)

    assert payload["properties"]["Tags"] == {
        "multi_select": [
            {"name": "langgraph"},
            {"name": "multi-agent"},
            {"name": "orchestration"},
        ]
    }


def test_topic_tags_drop_duplicates_and_commas():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="Low",
        topics=["rag", "RAG", "vector, search"],
        tasks=["Read"],
    )

    assert writer.build_project_payload(draft)["properties"]["Tags"] == {
        "multi_select": [{"name": "rag"}, {"name": "vector search"}]
    }


def test_task_gets_a_due_date_from_priority_and_carries_the_source():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_task_payload(
        task_name="Explore",
        priority="High",
        project_id="project-page-id",
        source_url="https://example.com/adk",
        today=date(2026, 7, 26),
    )

    properties = payload["properties"]
    assert properties["Due"] == {"date": {"start": "2026-07-29"}}
    assert properties["Source"] == {"url": "https://example.com/adk"}


def test_medium_priority_tasks_get_a_softer_due_date():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_task_payload(
        task_name="Read",
        priority="Medium",
        project_id="project-page-id",
        today=date(2026, 7, 26),
    )

    assert payload["properties"]["Due"] == {"date": {"start": "2026-08-09"}}


def test_low_priority_tasks_are_left_unscheduled():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )

    payload = writer.build_task_payload(
        task_name="Read",
        priority="Low",
        project_id="project-page-id",
        today=date(2026, 7, 26),
    )

    assert "Due" not in payload["properties"]


def test_task_properties_are_filtered_against_the_tasks_database():
    client = FakeClient(
        task_properties={"Task name", "Status", "Priority", "Projects"},
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="High",
        source_url="https://example.com",
        tasks=["Read"],
    )

    asyncio.run(writer.create_project_with_tasks(draft))

    _, task_payload = client.posts[1]
    assert set(task_payload["properties"]) == {
        "Task name",
        "Status",
        "Priority",
        "Projects",
    }


def test_a_project_page_never_sends_a_template_alongside_its_body():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="Low",
        tasks=["Read"],
    )

    payload = writer.build_project_payload(draft)

    assert payload["children"]
    assert "template" not in payload


def test_notion_error_message_reaches_the_caller():
    class FailingClient(FakeClient):
        async def post(self, url: str, *, headers, json) -> FakeResponse:
            return FakeResponse(
                {
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": "Tags is expected to be multi_select.",
                },
                status_code=400,
            )

    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        tasks_database_id="tasks-db",
        client=FailingClient(),
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        resource_type="article",
        intent="learn",
        priority="Low",
        tasks=["Read"],
    )

    with pytest.raises(NotionApiError) as failure:
        asyncio.run(writer.create_project_with_tasks(draft))

    assert "Tags is expected to be multi_select." in str(failure.value)
    assert "400" in str(failure.value)

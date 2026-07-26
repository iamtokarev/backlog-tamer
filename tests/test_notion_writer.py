from __future__ import annotations

import asyncio
from typing import Any

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.integrations.notion.writer import NotionWriter


class FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    """Records what the writer sends, and how many clients it needed."""

    def __init__(self):
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.open_requests = 0
        self.max_concurrent_requests = 0

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

    payload = writer.build_project_payload(draft)

    assert payload["parent"] == {"database_id": "projects-db"}
    assert payload["template"] == {"type": "default"}
    properties = payload["properties"]
    assert (
        properties["Project name"]["title"][0]["text"]["content"]
        == "Learn ADK callbacks"
    )
    assert properties["Status"] == {"status": {"name": "Backlog"}}
    assert properties["Priority"] == {"select": {"name": "Medium"}}
    assert properties["Tags"] == {
        "multi_select": [{"name": "documentation"}, {"name": "learn"}]
    }
    assert (
        properties["Summary"]["rich_text"][0]["text"]["content"]
        == "Understand how callbacks fit the approval workflow.\n\n"
        "Source: https://example.com/adk\n"
        "Type: documentation\n"
        "Intent: learn"
    )


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

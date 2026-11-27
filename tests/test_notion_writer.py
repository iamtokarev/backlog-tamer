from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from backlog_tamer.domain import (
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
    "Project type",
    "Intent",
    "Source",
    "Tags",
    "Captured",
    "Summary",
}


class FakeClient:
    """Records what the writer sends, and how many requests overlapped."""

    def __init__(
        self,
        project_properties: set[str] | None = None,
    ):
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.project_properties = (
            ALL_PROJECT_PROPERTIES if project_properties is None else project_properties
        )
        self.open_requests = 0
        self.max_concurrent_requests = 0

    async def get(self, url: str, *, headers) -> FakeResponse:
        self.gets.append(url)
        names = self.project_properties if "projects-db" in url else set()
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
        project_type="tool",
        intent="learn",
        priority="Medium",
        source_url="https://example.com/adk",
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
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
    assert properties["Project type"] == {"select": {"name": "tool"}}
    assert properties["Intent"] == {"select": {"name": "learn"}}
    assert properties["Source"] == {"url": "https://example.com/adk"}
    assert properties["Captured"] == {"date": {"start": "2026-07-26"}}
    # No type/intent fallback: those are their own properties now.
    assert properties["Tags"] == {"multi_select": []}


def test_summary_property_carries_only_the_summary():
    draft = ProjectDraft(
        project_name="Learn ADK callbacks",
        summary="Understand how callbacks fit the approval workflow.",
        project_type="tool",
        intent="learn",
        priority="Medium",
        source_url="https://example.com/adk",
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
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
        project_type="product",
        intent="build",
        priority="Low",
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
    )

    assert "Source" not in writer.build_project_payload(draft)["properties"]


def test_project_page_icon_follows_the_project_type():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
    )

    icons = {
        project_type: writer.build_project_payload(
            ProjectDraft(
                project_name="Example",
                summary="Example summary.",
                project_type=project_type,
                intent="learn",
                priority="Low",
            )
        )["icon"]["emoji"]
        for project_type in (
            "paper",
            "repository",
            "product",
            "company",
            "model",
            "tool",
        )
    }

    assert icons == {
        "paper": "🧪",
        "repository": "📦",
        "product": "🧩",
        "company": "🏢",
        "model": "🧠",
        "tool": "🛠️",
    }


def test_commit_skips_properties_the_database_does_not_have():
    client = FakeClient(project_properties={"Project name", "Status", "Summary"})
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="High",
        source_url="https://example.com",
    )

    asyncio.run(writer.create_project(draft))

    _, project_payload = client.posts[0]
    assert set(project_payload["properties"]) == {"Project name", "Status", "Summary"}
    assert project_payload["icon"] == {"type": "emoji", "emoji": "🧩"}


def test_commit_uses_legacy_type_column_until_notion_is_renamed():
    client = FakeClient(
        project_properties=(ALL_PROJECT_PROPERTIES - {"Project type"}) | {"Type"}
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Cursor Origin: explore an agentic git forge",
        summary="A git forge designed for agent-driven development.",
        project_type="product",
        intent="explore",
        priority="Medium",
        source_url="https://cursor.com/origin",
    )

    asyncio.run(writer.create_project(draft))

    _, project_payload = client.posts[0]
    assert "Project type" not in project_payload["properties"]
    assert project_payload["properties"]["Type"] == {"select": {"name": "product"}}


def test_commit_reads_the_schema_once_per_writer():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="High",
    )

    asyncio.run(writer.create_project(draft))
    asyncio.run(writer.create_project(draft))

    assert client.gets == ["https://api.notion.com/v1/databases/projects-db"]


def test_schema_report_is_healthy_when_every_property_exists():
    client = FakeClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )

    report = asyncio.run(writer.describe_schema())

    assert report.is_healthy
    assert report.missing_project_properties == []
    assert client.gets == ["https://api.notion.com/v1/databases/projects-db"]
    assert report.skipped_project_properties == []


def test_schema_report_separates_renamed_columns_from_not_yet_added_ones():
    client = FakeClient(
        project_properties={"Status", "Priority", "Tags", "Summary"},
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )

    report = asyncio.run(writer.describe_schema())

    assert not report.is_healthy
    assert report.missing_project_properties == ["Project name"]
    assert report.skipped_project_properties == [
        "Captured",
        "Intent",
        "Project type",
        "Source",
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
    )
    draft = ProjectDraft(
        project_name="LangGraph: build stateful multi-agent workflows",
        summary="Graph of stateful nodes.",
        project_type="tool",
        intent="build",
        priority="High",
        source_url="https://blog.langchain.com/langgraph/",
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

    assert _children_of(payload, "to_do") == []
    assert "Next action" not in [
        _plain_text(block) for block in _children_of(payload, "heading_3")
    ]


def test_page_body_skips_the_note_when_the_message_was_only_a_link():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="Low",
        source_url="https://example.com",
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
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="build",
        priority="Low",
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
    )
    draft = ProjectDraft(
        project_name="LangGraph: build stateful multi-agent workflows",
        summary="Graph of stateful nodes.",
        project_type="tool",
        intent="build",
        priority="High",
        topics=["LangGraph", "Multi-Agent", "orchestration"],
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
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="Low",
        topics=["rag", "RAG", "vector, search"],
    )

    assert writer.build_project_payload(draft)["properties"]["Tags"] == {
        "multi_select": [{"name": "rag"}, {"name": "vector search"}]
    }


def test_a_project_page_never_sends_a_template_alongside_its_body():
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="Low",
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
        client=FailingClient(),
    )
    draft = ProjectDraft(
        project_name="Example",
        summary="Example summary.",
        project_type="product",
        intent="learn",
        priority="Low",
    )

    with pytest.raises(NotionApiError) as failure:
        asyncio.run(writer.create_project(draft))

    assert "Tags is expected to be multi_select." in str(failure.value)
    assert "400" in str(failure.value)


class TagVocabularyClient(FakeClient):
    """A projects database whose Tags column already has options."""

    def __init__(self, tag_names: list[str]):
        super().__init__()
        self.tag_names = tag_names

    async def get(self, url: str, *, headers) -> FakeResponse:
        self.gets.append(url)
        properties: dict[str, Any] = {
            name: {} for name in self.project_properties if name != "Tags"
        }
        properties["Tags"] = {
            "multi_select": {"options": [{"name": name} for name in self.tag_names]}
        }
        return FakeResponse({"properties": properties})


def test_source_stores_the_canonical_url_so_a_recapture_can_match_it():
    from backlog_tamer.domain import Grounding

    draft = ProjectDraft(
        project_name="LangChain Deep Agents: build long-running AI agents",
        summary="Course on long-running agents.",
        project_type="course",
        intent="learn",
        priority="Medium",
        source_url="https://academy.langchain.com/courses/deepagents?_gl=1*174rmm0",
    )
    grounding = Grounding(
        fetch_status="success",
        canonical_url="https://academy.langchain.com/courses/deepagents",
    )
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
    )

    payload = writer.build_project_payload(draft, grounding=grounding)

    assert payload["properties"]["Source"] == {
        "url": "https://academy.langchain.com/courses/deepagents"
    }


def test_schema_report_names_the_capability_a_missing_source_column_disables():
    client = FakeClient(project_properties=ALL_PROJECT_PROPERTIES - {"Source"})
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )

    report = asyncio.run(writer.describe_schema())

    assert report.skipped_project_properties == ["Source"]
    assert report.degraded_capabilities == ["duplicate-detection"]


def test_provenance_callout_names_the_date_site_and_triage():
    from backlog_tamer.domain import Grounding

    writer = NotionWriter(token="secret", projects_database_id="projects-db")
    draft = ProjectDraft(
        project_name="LangGraph",
        summary="Graph of stateful nodes.",
        project_type="repository",
        intent="build",
        priority="High",
        source_url="https://github.com/langchain-ai/langgraph",
    )

    payload = writer.build_project_payload(
        draft,
        captured_at=date(2026, 7, 26),
        grounding=Grounding(fetch_status="success", site_name="GitHub"),
    )

    assert _plain_text(_children_of(payload, "callout")[0]) == (
        "Captured via Telegram on 2026-07-26 · GitHub · triaged by backlog-tamer"
    )


def test_topic_tags_fold_spelling_variants_and_keep_at_most_three():
    writer = NotionWriter(token="secret", projects_database_id="projects-db")
    draft = ProjectDraft(
        project_name="ACES: evaluate agent skills continuously",
        summary="Continuous evaluation for reusable agent skills.",
        project_type="paper",
        intent="learn",
        priority="Medium",
        topics=["Agent-Evaluation", "agent evaluations", "LLM agents"],
    )

    tags = writer.build_project_payload(draft)["properties"]["Tags"]

    assert tags == {
        "multi_select": [{"name": "agent-evaluation"}, {"name": "llm agents"}]
    }


def test_commit_makes_one_page_and_ignores_the_existing_tag_vocabulary():
    client = TagVocabularyClient(["agent evaluation"])
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )
    draft = ProjectDraft(
        project_name="ACES",
        summary="Continuous evaluation for reusable agent skills.",
        project_type="paper",
        intent="learn",
        priority="Medium",
        topics=["Agent-Evaluation"],
    )

    result = asyncio.run(writer.create_project(draft))

    assert (result.project_id, result.project_url) == (
        "page-1",
        "https://notion.so/page-1",
    )
    assert len(client.posts) == 1
    tags = client.posts[0][1]["properties"]["Tags"]["multi_select"]
    assert tags == [{"name": "agent-evaluation"}]


def test_list_projects_follows_every_page_of_the_query():
    class PagedClient(FakeClient):
        async def post(self, url: str, *, headers, json) -> FakeResponse:
            self.posts.append((url, json))
            if "start_cursor" not in json:
                return FakeResponse(
                    {"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}
                )
            return FakeResponse({"results": [{"id": "b"}], "has_more": False})

    client = PagedClient()
    writer = NotionWriter(
        token="secret",
        projects_database_id="projects-db",
        client=client,
    )

    pages = asyncio.run(writer.list_projects())

    assert [page["id"] for page in pages] == ["a", "b"]
    assert client.posts[0][0].endswith("/databases/projects-db/query")
    assert client.posts[1][1]["start_cursor"] == "c1"

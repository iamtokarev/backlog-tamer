from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.integrations.notion.writer import NotionWriter


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

    assert payload["properties"]["Tags"] == {
        "multi_select": [{"name": "explore"}]
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
    properties = payload["properties"]
    assert properties["Task name"]["title"][0]["text"]["content"] == "Read docs"
    assert properties["Status"] == {"status": {"name": "Not started"}}
    assert properties["Priority"] == {"select": {"name": "High"}}
    assert properties["Projects"] == {"relation": [{"id": "project-page-id"}]}

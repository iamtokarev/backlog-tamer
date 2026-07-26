from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.config import Settings

NOTION_API_BASE_URL = "https://api.notion.com/v1"
PROJECT_STATUS = "Backlog"
TASK_STATUS = "Not started"

RESOURCE_TYPE_EMOJI = {
    "article": "📄",
    "paper": "🧪",
    "video": "🎬",
    "course": "🎓",
    "documentation": "📘",
    "repository": "📦",
    "idea": "💡",
    "unknown": "❔",
}


@dataclass(frozen=True)
class NotionCommitResult:
    project_id: str
    project_url: str
    task_ids: list[str]


class NotionWriter:
    def __init__(
        self,
        *,
        token: str,
        projects_database_id: str,
        tasks_database_id: str,
        api_version: str = "2022-06-28",
        client: httpx.AsyncClient | None = None,
    ):
        self.token = token
        self.projects_database_id = projects_database_id
        self.tasks_database_id = tasks_database_id
        self.api_version = api_version
        self.client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> NotionWriter:
        if settings.notion_token is None:
            raise ValueError("NOTION_TOKEN must be configured to write to Notion.")
        return cls(
            token=settings.notion_token.get_secret_value(),
            projects_database_id=settings.notion_projects_database_id,
            tasks_database_id=settings.notion_tasks_database_id,
            api_version=settings.notion_api_version,
        )

    async def create_project_with_tasks(
        self,
        draft: ProjectDraft,
    ) -> NotionCommitResult:
        project_payload = self.build_project_payload(draft)
        project = await self._post_page(project_payload)
        project_id = _require_text(project, "id")
        project_url = _require_text(project, "url")

        task_ids: list[str] = []
        for task_name in draft.tasks:
            task_payload = self.build_task_payload(
                task_name=task_name,
                priority=draft.priority,
                project_id=project_id,
            )
            task = await self._post_page(task_payload)
            task_ids.append(_require_text(task, "id"))

        return NotionCommitResult(
            project_id=project_id,
            project_url=project_url,
            task_ids=task_ids,
        )

    def build_project_payload(self, draft: ProjectDraft) -> dict[str, Any]:
        summary = draft.summary
        if draft.source_url:
            summary = f"{summary}\n\nSource: {draft.source_url}"
        summary = f"{summary}\nType: {draft.resource_type}\nIntent: {draft.intent}"

        return {
            "parent": {"database_id": self.projects_database_id},
            "template": {"type": "default"},
            "icon": _emoji(RESOURCE_TYPE_EMOJI.get(draft.resource_type, "❔")),
            "properties": {
                "Project name": _title(draft.project_name),
                "Status": _status(PROJECT_STATUS),
                "Priority": _select(draft.priority),
                "Tags": {"multi_select": _draft_tags(draft)},
                "Summary": _rich_text(summary),
            },
        }

    def build_task_payload(
        self,
        *,
        task_name: str,
        priority: str,
        project_id: str,
    ) -> dict[str, Any]:
        return {
            "parent": {"database_id": self.tasks_database_id},
            "template": {"type": "default"},
            "properties": {
                "Task name": _title(task_name),
                "Status": _status(TASK_STATUS),
                "Priority": _select(priority),
                "Projects": {"relation": [{"id": project_id}]},
            },
        }

    async def _post_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.client is not None:
            response = await self.client.post(
                f"{NOTION_API_BASE_URL}/pages",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{NOTION_API_BASE_URL}/pages",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Notion-Version": self.api_version,
        }


def _emoji(value: str) -> dict[str, Any]:
    return {"type": "emoji", "emoji": value}


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def _rich_text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


def _status(value: str) -> dict[str, Any]:
    return {"status": {"name": value}}


def _draft_tags(draft: ProjectDraft) -> list[dict[str, str]]:
    tags: list[str] = []
    if draft.resource_type != "unknown":
        tags.append(draft.resource_type)
    tags.append("explore" if draft.intent == "unclear" else draft.intent)
    return [{"name": tag} for tag in tags]


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Notion response did not include a valid {key!r}.")
    return value

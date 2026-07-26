from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.config import Settings

logger = logging.getLogger(__name__)

NOTION_API_BASE_URL = "https://api.notion.com/v1"
PROJECT_STATUS = "Backlog"
TASK_STATUS = "Not started"

# Notion property names, in one place: renaming a column in Notion is a
# one-line change here, and the healthcheck reports the mismatch.
PROJECT_NAME_PROPERTY = "Project name"
PROJECT_STATUS_PROPERTY = "Status"
PROJECT_PRIORITY_PROPERTY = "Priority"
PROJECT_SOURCE_PROPERTY = "Source"
PROJECT_TYPE_PROPERTY = "Type"
PROJECT_INTENT_PROPERTY = "Intent"
PROJECT_TAGS_PROPERTY = "Tags"
PROJECT_CAPTURED_PROPERTY = "Captured"
PROJECT_SUMMARY_PROPERTY = "Summary"

TASK_NAME_PROPERTY = "Task name"
TASK_STATUS_PROPERTY = "Status"
TASK_PRIORITY_PROPERTY = "Priority"
TASK_PROJECT_PROPERTY = "Projects"

# Properties the writer sends but a database may legitimately not have yet.
# Anything outside this set is required and reported as missing.
OPTIONAL_PROJECT_PROPERTIES = frozenset(
    {
        PROJECT_SOURCE_PROPERTY,
        PROJECT_TYPE_PROPERTY,
        PROJECT_INTENT_PROPERTY,
        PROJECT_CAPTURED_PROPERTY,
    }
)
OPTIONAL_TASK_PROPERTIES: frozenset[str] = frozenset()

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


@dataclass(frozen=True)
class NotionSchemaReport:
    missing_project_properties: list[str]
    missing_task_properties: list[str]
    skipped_project_properties: list[str]

    @property
    def is_healthy(self) -> bool:
        return not self.missing_project_properties and not self.missing_task_properties


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
        self._project_property_cache: set[str] | None = None

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
        async with self._session() as client:
            payload = await self._fit_to_schema(
                client,
                self.build_project_payload(draft),
            )
            project = await self._post_page(client, payload)
            project_id = _require_text(project, "id")
            project_url = _require_text(project, "url")

            tasks = await asyncio.gather(
                *(
                    self._post_page(
                        client,
                        self.build_task_payload(
                            task_name=task_name,
                            priority=draft.priority,
                            project_id=project_id,
                        ),
                    )
                    for task_name in draft.tasks
                )
            )

        return NotionCommitResult(
            project_id=project_id,
            project_url=project_url,
            task_ids=[_require_text(task, "id") for task in tasks],
        )

    def build_project_payload(
        self,
        draft: ProjectDraft,
        captured_at: date | None = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {
            PROJECT_NAME_PROPERTY: _title(draft.project_name),
            PROJECT_STATUS_PROPERTY: _status(PROJECT_STATUS),
            PROJECT_PRIORITY_PROPERTY: _select(draft.priority),
            PROJECT_TYPE_PROPERTY: _select(draft.resource_type),
            PROJECT_INTENT_PROPERTY: _select(draft.intent),
            PROJECT_TAGS_PROPERTY: {"multi_select": _draft_tags(draft)},
            PROJECT_CAPTURED_PROPERTY: _date(captured_at or date.today()),
            PROJECT_SUMMARY_PROPERTY: _rich_text(draft.summary),
        }
        if draft.source_url:
            properties[PROJECT_SOURCE_PROPERTY] = {"url": draft.source_url}

        return {
            "parent": {"database_id": self.projects_database_id},
            "template": {"type": "default"},
            "icon": _emoji(RESOURCE_TYPE_EMOJI.get(draft.resource_type, "❔")),
            "properties": properties,
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
                TASK_NAME_PROPERTY: _title(task_name),
                TASK_STATUS_PROPERTY: _status(TASK_STATUS),
                TASK_PRIORITY_PROPERTY: _select(priority),
                TASK_PROJECT_PROPERTY: {"relation": [{"id": project_id}]},
            },
        }

    async def _fit_to_schema(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Drop properties the Projects database does not have.

        Source, Type, Intent and Captured are recent additions; a workspace
        that has not added the columns yet still gets a usable page instead of
        a 400 at commit time.
        """
        known = await self._known_project_properties(client)
        if known is None:
            return payload

        properties = payload["properties"]
        unknown = sorted(set(properties) - known)
        if not unknown:
            return payload

        logger.warning(
            "Skipping Notion properties missing from the Projects database: %s",
            ", ".join(unknown),
        )
        return {
            **payload,
            "properties": {
                name: value for name, value in properties.items() if name in known
            },
        }

    async def _known_project_properties(
        self,
        client: httpx.AsyncClient,
    ) -> set[str] | None:
        if self._project_property_cache is not None:
            return self._project_property_cache
        try:
            self._project_property_cache = await self._database_properties(
                client,
                self.projects_database_id,
            )
        except Exception:
            # Never block a commit on the probe: send everything and let
            # Notion be the judge.
            logger.warning("Could not read the Projects database schema.")
            return None
        return self._project_property_cache

    async def describe_schema(self) -> NotionSchemaReport:
        """Compare what the writer sends against what the databases have.

        Called from the healthcheck: a property renamed in Notion otherwise
        fails as a 400 during finalize_approval, after the user approved and
        the confirmation is already marked COMMITTING.
        """
        async with self._session() as client:
            project_properties = await self._database_properties(
                client,
                self.projects_database_id,
            )
            task_properties = await self._database_properties(
                client,
                self.tasks_database_id,
            )

        sample = ProjectDraft(
            project_name="schema probe",
            summary="schema probe",
            resource_type="article",
            intent="learn",
            priority="Medium",
            source_url="https://example.com",
            tasks=["probe"],
        )
        wanted_project = set(self.build_project_payload(sample)["properties"])
        wanted_task = set(
            self.build_task_payload(
                task_name="probe",
                priority="Medium",
                project_id="probe",
            )["properties"]
        )

        return NotionSchemaReport(
            missing_project_properties=sorted(
                (wanted_project - project_properties) - OPTIONAL_PROJECT_PROPERTIES
            ),
            missing_task_properties=sorted(
                (wanted_task - task_properties) - OPTIONAL_TASK_PROPERTIES
            ),
            skipped_project_properties=sorted(
                (wanted_project - project_properties) & OPTIONAL_PROJECT_PROPERTIES
            ),
        )

    async def _database_properties(
        self,
        client: httpx.AsyncClient,
        database_id: str,
    ) -> set[str]:
        response = await client.get(
            f"{NOTION_API_BASE_URL}/databases/{database_id}",
            headers=self._headers(),
        )
        response.raise_for_status()
        properties = response.json().get("properties", {})
        return set(properties) if isinstance(properties, dict) else set()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """One client per commit: every page of a commit shares the connection."""
        if self.client is not None:
            yield self.client
            return
        async with httpx.AsyncClient(timeout=20.0) as client:
            yield client

    async def _post_page(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
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


def _date(value: date) -> dict[str, Any]:
    return {"date": {"start": value.isoformat()}}


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

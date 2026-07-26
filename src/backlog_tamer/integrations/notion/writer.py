from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)
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
TASK_DUE_PROPERTY = "Due"
TASK_SOURCE_PROPERTY = "Source"

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
OPTIONAL_TASK_PROPERTIES = frozenset({TASK_DUE_PROPERTY, TASK_SOURCE_PROPERTY})

# A soft first-touch date, so an item has a "when" and can be scheduled.
PRIORITY_DUE_DAYS = {"High": 3, "Medium": 14, "Low": None}

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
class ExistingProject:
    page_id: str
    page_url: str
    created_time: str | None = None


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
        self._property_cache: dict[str, set[str]] = {}

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
        incoming_context: IncomingContext | None = None,
        grounding: DraftGrounding | None = None,
    ) -> NotionCommitResult:
        async with self._session() as client:
            payload = await self._fit_to_schema(
                client,
                self.projects_database_id,
                self.build_project_payload(
                    draft,
                    incoming_context=incoming_context,
                    grounding=grounding,
                ),
            )
            project = await self._post_page(client, payload)
            project_id = _require_text(project, "id")
            project_url = _require_text(project, "url")

            task_payloads = [
                await self._fit_to_schema(
                    client,
                    self.tasks_database_id,
                    self.build_task_payload(
                        task_name=task_name,
                        priority=draft.priority,
                        project_id=project_id,
                        source_url=draft.source_url,
                    ),
                )
                for task_name in draft.tasks
            ]
            tasks = await asyncio.gather(
                *(self._post_page(client, payload) for payload in task_payloads)
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
        incoming_context: IncomingContext | None = None,
        grounding: DraftGrounding | None = None,
    ) -> dict[str, Any]:
        captured_on = captured_at or date.today()
        properties: dict[str, Any] = {
            PROJECT_NAME_PROPERTY: _title(draft.project_name),
            PROJECT_STATUS_PROPERTY: _status(PROJECT_STATUS),
            PROJECT_PRIORITY_PROPERTY: _select(draft.priority),
            PROJECT_TYPE_PROPERTY: _select(draft.resource_type),
            PROJECT_INTENT_PROPERTY: _select(draft.intent),
            PROJECT_TAGS_PROPERTY: {"multi_select": _draft_tags(draft)},
            PROJECT_CAPTURED_PROPERTY: _date(captured_on),
            PROJECT_SUMMARY_PROPERTY: _rich_text(draft.summary),
        }
        if draft.source_url:
            properties[PROJECT_SOURCE_PROPERTY] = {"url": draft.source_url}

        return {
            "parent": {"database_id": self.projects_database_id},
            "template": {"type": "default"},
            "icon": _emoji(RESOURCE_TYPE_EMOJI.get(draft.resource_type, "❔")),
            "properties": properties,
            "children": build_project_children(
                draft,
                incoming_context=incoming_context,
                grounding=grounding,
                captured_on=captured_on,
            ),
        }

    def build_task_payload(
        self,
        *,
        task_name: str,
        priority: str,
        project_id: str,
        source_url: str | None = None,
        today: date | None = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {
            TASK_NAME_PROPERTY: _title(task_name),
            TASK_STATUS_PROPERTY: _status(TASK_STATUS),
            TASK_PRIORITY_PROPERTY: _select(priority),
            TASK_PROJECT_PROPERTY: {"relation": [{"id": project_id}]},
        }

        due_on = _due_date(priority, today)
        if due_on is not None:
            properties[TASK_DUE_PROPERTY] = _date(due_on)
        if source_url:
            # So the task is actionable without opening the project first.
            properties[TASK_SOURCE_PROPERTY] = {"url": source_url}

        return {
            "parent": {"database_id": self.tasks_database_id},
            "template": {"type": "default"},
            "properties": properties,
        }

    async def find_project_by_source(self, source_url: str) -> ExistingProject | None:
        """Look for a project already saved from this URL.

        Nothing else stops the same link becoming three projects, which is
        exactly how the backlog turns back into an inbox.
        """
        async with self._session() as client:
            known = await self._known_properties(client, self.projects_database_id)
            if known is not None and PROJECT_SOURCE_PROPERTY not in known:
                return None

            try:
                response = await client.post(
                    f"{NOTION_API_BASE_URL}/databases/"
                    f"{self.projects_database_id}/query",
                    headers=self._headers(),
                    json={
                        "filter": {
                            "property": PROJECT_SOURCE_PROPERTY,
                            "url": {"equals": source_url},
                        },
                        "page_size": 1,
                    },
                )
                response.raise_for_status()
                results = response.json().get("results") or []
            except Exception:
                # A duplicate check is a convenience; never block the commit.
                logger.warning("Duplicate lookup failed for %s.", source_url)
                return None

        if not results:
            return None
        return ExistingProject(
            page_id=_require_text(results[0], "id"),
            page_url=_require_text(results[0], "url"),
            created_time=results[0].get("created_time"),
        )

    async def add_tasks_to_project(
        self,
        *,
        project_id: str,
        draft: ProjectDraft,
    ) -> list[str]:
        """Attach this draft's tasks to a project that already exists."""
        async with self._session() as client:
            payloads = [
                await self._fit_to_schema(
                    client,
                    self.tasks_database_id,
                    self.build_task_payload(
                        task_name=task_name,
                        priority=draft.priority,
                        project_id=project_id,
                        source_url=draft.source_url,
                    ),
                )
                for task_name in draft.tasks
            ]
            tasks = await asyncio.gather(
                *(self._post_page(client, payload) for payload in payloads)
            )
        return [_require_text(task, "id") for task in tasks]

    async def archive_pages(self, page_ids: list[str]) -> None:
        """Undo a commit. Notion archives pages rather than deleting them."""
        async with self._session() as client:
            await asyncio.gather(
                *(
                    client.patch(
                        f"{NOTION_API_BASE_URL}/pages/{page_id}",
                        headers=self._headers(),
                        json={"archived": True},
                    )
                    for page_id in page_ids
                )
            )

    async def _fit_to_schema(
        self,
        client: httpx.AsyncClient,
        database_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Drop properties the target database does not have.

        Source, Type, Intent, Captured and Due date are recent additions; a
        workspace that has not added the columns yet still gets a usable page
        instead of a 400 at commit time.
        """
        known = await self._known_properties(client, database_id)
        if known is None:
            return payload

        properties = payload["properties"]
        unknown = sorted(set(properties) - known)
        if not unknown:
            return payload

        logger.warning(
            "Skipping Notion properties missing from database %s: %s",
            database_id,
            ", ".join(unknown),
        )
        return {
            **payload,
            "properties": {
                name: value for name, value in properties.items() if name in known
            },
        }

    async def _known_properties(
        self,
        client: httpx.AsyncClient,
        database_id: str,
    ) -> set[str] | None:
        if database_id in self._property_cache:
            return self._property_cache[database_id]
        try:
            self._property_cache[database_id] = await self._database_properties(
                client,
                database_id,
            )
        except Exception:
            # Never block a commit on the probe: send everything and let
            # Notion be the judge.
            logger.warning("Could not read the schema of database %s.", database_id)
            return None
        return self._property_cache[database_id]

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
                source_url="https://example.com",
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


def build_project_children(
    draft: ProjectDraft,
    *,
    incoming_context: IncomingContext | None = None,
    grounding: DraftGrounding | None = None,
    captured_on: date | None = None,
) -> list[dict[str, Any]]:
    """The page body.

    Without this a link-shaped item lands as a database row with an empty
    page, which is the "another inbox" failure mode the product is meant to
    prevent.
    """
    children: list[dict[str, Any]] = []

    if draft.source_url:
        children.append(
            {
                "object": "block",
                "type": "bookmark",
                "bookmark": {"url": draft.source_url},
            }
        )

    note = _capture_note(incoming_context, draft.source_url)
    if note:
        children.append(_heading("Why I saved this"))
        children.append(_paragraph(note))

    if grounding is not None and grounding.key_points:
        children.append(_heading("Key points"))
        children.extend(_bulleted(point) for point in grounding.key_points)

    if draft.tasks:
        children.append(_heading("Next action"))
        children.extend(_to_do(task) for task in draft.tasks)

    provenance = [
        f"Captured via Telegram on {(captured_on or date.today()).isoformat()}"
    ]
    if grounding is not None and grounding.site_name:
        provenance.append(grounding.site_name)
    provenance.append("drafted by intake_triage")
    children.append(_callout("🤖", " · ".join(provenance)))
    return children


def _capture_note(
    incoming_context: IncomingContext | None,
    source_url: str | None,
) -> str | None:
    """The user's own words, which recall the item better than a summary."""
    if incoming_context is None:
        return None
    note = (incoming_context.note or "").strip()
    if not note:
        raw = incoming_context.raw_text.strip()
        note = "" if raw == (source_url or "").strip() else raw
    return note or None


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": _text_fragments(text)},
    }


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _text_fragments(text)},
    }


def _bulleted(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _text_fragments(text)},
    }


def _to_do(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": _text_fragments(text), "checked": False},
    }


def _callout(emoji: str, text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _text_fragments(text),
            "icon": _emoji(emoji),
        },
    }


def _text_fragments(value: str) -> list[dict[str, Any]]:
    """Notion rejects any single text fragment over 2000 characters."""
    limit = 2000
    return [
        {"type": "text", "text": {"content": value[index : index + limit]}}
        for index in range(0, max(len(value), 1), limit)
    ]


def _due_date(priority: str, today: date | None = None) -> date | None:
    days = PRIORITY_DUE_DAYS.get(priority)
    if days is None:
        return None
    return (today or date.today()) + timedelta(days=days)


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
    """Tags carry topics now that Type and Intent are their own properties.

    Drafts written before topics existed fall back to the old behaviour so
    their tags do not come out empty.
    """
    if draft.topics:
        return [{"name": topic} for topic in _normalized_topics(draft.topics)]

    tags: list[str] = []
    if draft.resource_type != "unknown":
        tags.append(draft.resource_type)
    tags.append("explore" if draft.intent == "unclear" else draft.intent)
    return [{"name": tag} for tag in tags]


def _normalized_topics(topics: list[str]) -> list[str]:
    """Lowercase, de-duplicated, and free of the commas Notion splits on."""
    seen: list[str] = []
    for topic in topics:
        cleaned = topic.strip().lower().replace(",", " ")
        cleaned = " ".join(cleaned.split())
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen[:3]


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Notion response did not include a valid {key!r}.")
    return value

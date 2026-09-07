from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
PROJECT_TYPE_PROPERTY = "Project type"
LEGACY_PROJECT_TYPE_PROPERTY = "Type"
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

PROJECT_TYPE_EMOJI = {
    "paper": "🧪",
    "article": "📰",
    "video": "🎬",
    "course": "🎓",
    "repository": "📦",
    "product": "🧩",
    "company": "🏢",
    "model": "🧠",
    "tool": "🛠️",
}

# Capabilities that quietly stop working when an optional column is absent,
# rather than just leaving a field blank.
PROPERTY_CAPABILITIES = {PROJECT_SOURCE_PROPERTY: "duplicate-detection"}


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
    degraded_capabilities: list[str] = field(default_factory=list)

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
        self._schema_cache: dict[str, dict[str, Any]] = {}

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
        known_tags = await self.list_tag_options()
        async with self._session() as client:
            payload = await self._fit_to_schema(
                client,
                self.projects_database_id,
                self.build_project_payload(
                    draft,
                    incoming_context=incoming_context,
                    grounding=grounding,
                    known_tags=known_tags,
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
                        source_url=commit_source_url(draft, grounding),
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
        known_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        captured_on = captured_at or date.today()
        source_url = commit_source_url(draft, grounding)
        properties: dict[str, Any] = {
            PROJECT_NAME_PROPERTY: _title(draft.project_name),
            PROJECT_STATUS_PROPERTY: _status(PROJECT_STATUS),
            PROJECT_PRIORITY_PROPERTY: _select(draft.priority),
            PROJECT_TYPE_PROPERTY: _select(draft.project_type),
            PROJECT_INTENT_PROPERTY: _select(draft.intent),
            PROJECT_TAGS_PROPERTY: {"multi_select": _draft_tags(draft, known_tags)},
            PROJECT_CAPTURED_PROPERTY: _date(captured_on),
            PROJECT_SUMMARY_PROPERTY: _rich_text(draft.summary),
        }
        if source_url:
            properties[PROJECT_SOURCE_PROPERTY] = {"url": source_url}

        # No "template" here: Notion rejects a page that sends both a template
        # and children, and the body we build is the point of the page.
        return {
            "parent": {"database_id": self.projects_database_id},
            "icon": _emoji(PROJECT_TYPE_EMOJI.get(draft.project_type, "❔")),
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

    async def list_tag_options(self) -> list[str]:
        """The Tags values the workspace already uses.

        Fed to the drafting prompt so the agent reuses an existing tag instead
        of minting a near-duplicate; the vocabulary had already split into
        "agent evaluation"/"agent-evaluation" and two more such pairs.
        """
        try:
            async with self._session() as client:
                schema = await self._database_schema(
                    client,
                    self.projects_database_id,
                )
        except Exception:
            # A vocabulary hint is a nicety; never block a capture on it.
            logger.warning("Could not read the existing Notion tag vocabulary.")
            return []

        definition = schema.get(PROJECT_TAGS_PROPERTY) or {}
        options = (definition.get("multi_select") or {}).get("options") or []
        return [str(option["name"]) for option in options if option.get("name")]

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
                _raise_for_notion_error(response)
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
        grounding: DraftGrounding | None = None,
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
                        source_url=commit_source_url(draft, grounding),
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

        Source, Project type (formerly Type), Intent, Captured and Due date are
        recent additions; a
        workspace that has not added the columns yet still gets a usable page
        instead of a 400 at commit time.
        """
        known = await self._known_properties(client, database_id)
        if known is None:
            return payload

        properties = payload["properties"]
        if (
            database_id == self.projects_database_id
            and PROJECT_TYPE_PROPERTY in properties
            and PROJECT_TYPE_PROPERTY not in known
            and LEGACY_PROJECT_TYPE_PROPERTY in known
        ):
            properties = {
                (
                    LEGACY_PROJECT_TYPE_PROPERTY
                    if name == PROJECT_TYPE_PROPERTY
                    else name
                ): value
                for name, value in properties.items()
            }
        unknown = sorted(set(properties) - known)
        if not unknown:
            return {**payload, "properties": properties}

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
            project_type="product",
            intent="learn",
            priority="Medium",
            source_url="https://example.com",
            tasks=["probe"],
        )
        wanted_project = set(self.build_project_payload(sample)["properties"])
        if LEGACY_PROJECT_TYPE_PROPERTY in project_properties:
            project_properties.add(PROJECT_TYPE_PROPERTY)
        wanted_task = set(
            self.build_task_payload(
                task_name="probe",
                priority="Medium",
                project_id="probe",
                source_url="https://example.com",
            )["properties"]
        )

        skipped_project = sorted(
            (wanted_project - project_properties) & OPTIONAL_PROJECT_PROPERTIES
        )
        degraded = sorted(
            {
                PROPERTY_CAPABILITIES[name]
                for name in skipped_project
                if name in PROPERTY_CAPABILITIES
            }
        )
        if degraded:
            # A blank column is cosmetic; a disabled capability is not.
            logger.warning(
                "Notion schema gaps have disabled: %s. Missing properties: %s.",
                ", ".join(degraded),
                ", ".join(skipped_project),
            )

        return NotionSchemaReport(
            missing_project_properties=sorted(
                (wanted_project - project_properties) - OPTIONAL_PROJECT_PROPERTIES
            ),
            missing_task_properties=sorted(
                (wanted_task - task_properties) - OPTIONAL_TASK_PROPERTIES
            ),
            skipped_project_properties=skipped_project,
            degraded_capabilities=degraded,
        )

    async def _database_properties(
        self,
        client: httpx.AsyncClient,
        database_id: str,
    ) -> set[str]:
        return set(await self._database_schema(client, database_id))

    async def _database_schema(
        self,
        client: httpx.AsyncClient,
        database_id: str,
    ) -> dict[str, Any]:
        """The database's property definitions, fetched once per writer.

        Both the schema check and the tag vocabulary need this payload, and a
        commit should not pay for the same GET twice.
        """
        if database_id in self._schema_cache:
            return self._schema_cache[database_id]

        response = await client.get(
            f"{NOTION_API_BASE_URL}/databases/{database_id}",
            headers=self._headers(),
        )
        _raise_for_notion_error(response)
        properties = response.json().get("properties", {})
        schema = properties if isinstance(properties, dict) else {}
        self._schema_cache[database_id] = schema
        return schema

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
        _raise_for_notion_error(response)
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Notion-Version": self.api_version,
        }


def commit_source_url(
    draft: ProjectDraft,
    grounding: DraftGrounding | None = None,
) -> str | None:
    """The URL to store, preferring the canonical one.

    find_project_by_source already *looks up* by canonical_url, so storing the
    raw source_url means a re-capture of the same page through a different
    tracking link never matches its own earlier row.
    """
    if grounding is not None and grounding.canonical_url:
        return grounding.canonical_url
    return draft.source_url


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


def _draft_tags(
    draft: ProjectDraft,
    known_tags: list[str] | None = None,
) -> list[dict[str, str]]:
    """Tags carry topics now that Type and Intent are their own properties.

    Drafts written before topics existed fall back to the old behaviour so
    their tags do not come out empty.
    """
    if draft.topics:
        return [
            {"name": topic} for topic in _normalized_topics(draft.topics, known_tags)
        ]

    tags: list[str] = []
    tags.append(draft.project_type)
    tags.append("explore" if draft.intent == "unclear" else draft.intent)
    return [{"name": tag} for tag in tags]


def _normalized_topics(
    topics: list[str],
    known_tags: list[str] | None = None,
) -> list[str]:
    """Lowercase, de-duplicated, and free of the commas Notion splits on.

    A topic that matches an existing tag once hyphens, case and a trailing
    plural are folded away is rewritten to that tag's exact spelling, so
    "agent-evaluation" stops becoming a second option beside "agent
    evaluation". Existing tags are never rewritten, only matched against.
    """
    existing = {_tag_key(tag): tag for tag in reversed(known_tags or [])}

    seen: list[str] = []
    for topic in topics:
        cleaned = topic.strip().lower().replace(",", " ")
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            continue
        cleaned = existing.get(_tag_key(cleaned), cleaned)
        if cleaned not in seen:
            seen.append(cleaned)
    return seen[:3]


def _tag_key(tag: str) -> str:
    """Fold spelling differences that should not create a second tag."""
    folded = re.sub(r"[^a-z0-9]+", " ", tag.casefold()).strip()
    return " ".join(
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in folded.split()
    )


class NotionApiError(RuntimeError):
    pass


def _raise_for_notion_error(response) -> None:
    """Surface Notion's explanation, not just the status line.

    A 400 from httpx reads "Client error '400 Bad Request' for url ...", which
    tells the user nothing about which property or block was rejected.
    """
    if response.status_code < 400:
        return

    message = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("code")

    raise NotionApiError(
        f"Notion rejected the write ({response.status_code}): "
        f"{message or 'no reason given'}"
    )


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Notion response did not include a valid {key!r}.")
    return value

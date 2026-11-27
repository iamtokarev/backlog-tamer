"""Test doubles shared across test modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from backlog_tamer.config import Settings
from backlog_tamer.domain import (
    Grounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.integrations.notion import ExistingProject, NotionCommitResult
from backlog_tamer.integrations.source_reader import FetchedUrl
from backlog_tamer.integrations.telegram.config import TelegramConfig


def build_settings(database_url: str = "sqlite:///:memory:") -> Settings:
    """Settings that never read the environment or a local .env file."""
    return Settings(
        _env_file=None,
        openrouter_api_key=SecretStr("test-openrouter-key"),
        browserbase_api_key=SecretStr("test-browserbase-key"),
        telegram=TelegramConfig(
            bot_token=SecretStr("test-bot-token"),
            allowed_user_id=42,
        ),
        database_url=database_url,
        notion_projects_database_id="test-projects-db",
    )


@dataclass
class TriageCall:
    operation: str
    args: dict[str, Any] = field(default_factory=dict)


class FakeTriage:
    """Stands in for Triage: returns queued Drafts in order.

    Every call is recorded with its arguments, so a test can check what the
    intake module asked for (which context, Grounding, Draft and feedback)
    without a model.
    """

    def __init__(self, *drafts: ProjectDraft):
        self.drafts = list(drafts)
        self.calls: list[TriageCall] = []

    def queue(self, *drafts: ProjectDraft) -> None:
        self.drafts.extend(drafts)

    async def classify(
        self,
        context: IncomingContext,
        grounding: Grounding,
        vocabulary: list[str],
    ) -> ProjectDraft:
        return self._next(
            "classify",
            context=context,
            grounding=grounding,
            vocabulary=vocabulary,
        )

    async def revise(
        self,
        context: IncomingContext,
        grounding: Grounding,
        draft: ProjectDraft,
        feedback: str,
        vocabulary: list[str],
    ) -> ProjectDraft:
        return self._next(
            "revise",
            context=context,
            grounding=grounding,
            draft=draft,
            feedback=feedback,
            vocabulary=vocabulary,
        )

    @property
    def operations(self) -> list[str]:
        return [call.operation for call in self.calls]

    def _next(self, operation: str, **args: Any) -> ProjectDraft:
        self.calls.append(TriageCall(operation, args))
        if not self.drafts:
            raise AssertionError(f"No Draft queued for {operation}.")
        return self.drafts.pop(0)


class FakeReader:
    """Stands in for SourceReader: returns queued reads, then the last one again."""

    def __init__(self, *fetched: FetchedUrl):
        self.fetched = list(fetched)
        self.urls: list[str] = []

    def queue(self, *fetched: FetchedUrl) -> None:
        self.fetched.extend(fetched)

    @property
    def reads(self) -> int:
        return len(self.urls)

    async def read(self, url: str) -> FetchedUrl:
        self.urls.append(url)
        if len(self.fetched) > 1:
            return self.fetched.pop(0)
        if self.fetched:
            return self.fetched[0]
        return FetchedUrl(status="success", requested_url=url)


class FakeNotionWriter:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[ProjectDraft] = []
        self.contexts: list[IncomingContext | None] = []
        self.groundings: list[Grounding | None] = []
        self.existing_project: ExistingProject | None = None
        self.archived: list[str] = []
        self.pages_created = 0
        self.tag_options: list[str] = []

    async def list_tag_options(self) -> list[str]:
        return list(self.tag_options)

    async def find_project_by_source(self, source_url: str) -> ExistingProject | None:
        return self.existing_project

    async def archive_pages(self, page_ids: list[str]) -> None:
        self.archived.extend(page_ids)

    async def create_project(
        self,
        draft: ProjectDraft,
        incoming_context: IncomingContext | None = None,
        grounding: Grounding | None = None,
    ) -> NotionCommitResult:
        self.calls.append(draft)
        self.contexts.append(incoming_context)
        self.groundings.append(grounding)
        if self.fail:
            raise RuntimeError("notion unavailable")
        self.pages_created += 1
        return NotionCommitResult(
            project_id="project-id",
            project_url="https://notion.so/project-id",
        )

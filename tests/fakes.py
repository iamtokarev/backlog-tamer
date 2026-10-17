"""Test doubles shared across test modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Checkpoint,
    TurnOutcome,
)
from backlog_tamer.config import Settings
from backlog_tamer.integrations.notion import ExistingProject, NotionCommitResult
from backlog_tamer.integrations.telegram.config import TelegramConfig


def build_settings(database_url: str = "sqlite:///:memory:") -> Settings:
    """Settings that never read the environment or a local .env file."""
    return Settings(
        _env_file=None,
        openrouter_api_key=SecretStr("test-openrouter-key"),
        telegram=TelegramConfig(
            bot_token=SecretStr("test-bot-token"),
            allowed_user_id=42,
        ),
        database_url=database_url,
        notion_projects_database_id="test-projects-db",
        notion_tasks_database_id="test-tasks-db",
        langsmith_tracing=False,
    )


@dataclass
class WorkflowCall:
    operation: str
    args: dict[str, Any] = field(default_factory=dict)


class ScriptedTriageWorkflow:
    """Stands in for TriageWorkflow: returns queued Turn outcomes in order.

    Every operation is recorded, so a test can check what the intake module
    asked for (which decision, which Checkpoint, which kept fields) without
    running ADK.
    """

    def __init__(self, *outcomes: TurnOutcome):
        self.outcomes = list(outcomes)
        self.calls: list[WorkflowCall] = []

    def queue(self, *outcomes: TurnOutcome) -> None:
        self.outcomes.extend(outcomes)

    async def start(
        self,
        context: IncomingContext,
        user_id: str,
        known_topics: list[str],
    ) -> TurnOutcome:
        return self._next(
            "start",
            context=context,
            user_id=user_id,
            known_topics=known_topics,
        )

    async def revise(
        self,
        checkpoint: Checkpoint,
        feedback: str,
        kept_fields: dict[str, str],
    ) -> TurnOutcome:
        return self._next(
            "revise",
            checkpoint=checkpoint,
            feedback=feedback,
            kept_fields=kept_fields,
        )

    async def approve(self, checkpoint: Checkpoint) -> TurnOutcome:
        return self._next("approve", checkpoint=checkpoint)

    async def reject(self, checkpoint: Checkpoint) -> TurnOutcome:
        return self._next("reject", checkpoint=checkpoint)

    @property
    def operations(self) -> list[str]:
        return [call.operation for call in self.calls]

    def _next(self, operation: str, **args: Any) -> TurnOutcome:
        self.calls.append(WorkflowCall(operation, args))
        if not self.outcomes:
            raise AssertionError(f"No Turn outcome queued for {operation}.")
        return self.outcomes.pop(0)


class FakeNotionWriter:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[ProjectDraft] = []
        self.contexts: list[IncomingContext | None] = []
        self.groundings: list[DraftGrounding | None] = []
        self.existing_project: ExistingProject | None = None
        self.archived: list[str] = []
        self.added_task_project_ids: list[str] = []
        self.tag_options: list[str] = []

    async def list_tag_options(self) -> list[str]:
        return list(self.tag_options)

    async def find_project_by_source(self, source_url: str) -> ExistingProject | None:
        return self.existing_project

    async def archive_pages(self, page_ids: list[str]) -> None:
        self.archived.extend(page_ids)

    async def add_tasks_to_project(
        self,
        *,
        project_id: str,
        draft: ProjectDraft,
        grounding: DraftGrounding | None = None,
    ):
        self.added_task_project_ids.append(project_id)
        return ["new-task-id"]

    async def create_project_with_tasks(
        self,
        draft: ProjectDraft,
        incoming_context: IncomingContext | None = None,
        grounding: DraftGrounding | None = None,
    ) -> NotionCommitResult:
        self.calls.append(draft)
        self.contexts.append(incoming_context)
        self.groundings.append(grounding)
        if self.fail:
            raise RuntimeError("notion unavailable")
        return NotionCommitResult(
            project_id="project-id",
            project_url="https://notion.so/project-id",
            task_ids=["task-id"],
        )

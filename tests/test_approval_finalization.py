from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.application.confirmation_store import ConfirmationStore, utc_now
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationRecord, ConfirmationStatus
from backlog_tamer.integrations.notion import ExistingProject, NotionCommitResult


class FakeNotionWriter:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[ProjectDraft] = []
        self.contexts: list[IncomingContext | None] = []
        self.groundings: list[DraftGrounding | None] = []
        self.existing_project: ExistingProject | None = None
        self.archived: list[str] = []
        self.added_task_project_ids: list[str] = []

    async def find_project_by_source(self, source_url: str) -> ExistingProject | None:
        return self.existing_project

    async def archive_pages(self, page_ids: list[str]) -> None:
        self.archived.extend(page_ids)

    async def add_tasks_to_project(self, *, project_id: str, draft: ProjectDraft):
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


def test_finalize_approval_commits_project_and_is_idempotent(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter()
    service = _build_service(store, writer)

    first = asyncio.run(service.finalize_approval(record.confirmation_id))
    second = asyncio.run(service.finalize_approval(record.confirmation_id))

    assert first.status == ConfirmationStatus.COMMITTED.value
    assert second.status == ConfirmationStatus.COMMITTED.value
    assert first.notion_project_url == "https://notion.so/project-id"
    assert second.notion_project_url == "https://notion.so/project-id"
    assert len(writer.calls) == 1

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.status == ConfirmationStatus.COMMITTED
    assert stored.notion_project_id == "project-id"
    assert stored.notion_project_url == "https://notion.so/project-id"


def test_finalize_approval_marks_failed_on_notion_error(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter(fail=True)
    service = _build_service(store, writer)

    result = asyncio.run(service.finalize_approval(record.confirmation_id))

    assert result.status == ConfirmationStatus.FAILED.value
    assert len(writer.calls) == 1
    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.status == ConfirmationStatus.FAILED
    assert stored.failure_reason == "notion unavailable"


def test_failed_confirmation_can_be_retried(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter(fail=True)
    service = _build_service(store, writer)

    failed = asyncio.run(service.finalize_approval(record.confirmation_id))
    writer.fail = False
    retried = asyncio.run(service.finalize_approval(record.confirmation_id))

    assert failed.status == ConfirmationStatus.FAILED.value
    assert failed.failure_reason == "notion unavailable"
    assert retried.status == ConfirmationStatus.COMMITTED.value
    assert retried.notion_project_url == "https://notion.so/project-id"
    assert len(writer.calls) == 2

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.status == ConfirmationStatus.COMMITTED
    assert stored.failure_reason is None


def test_rejected_confirmation_never_calls_notion(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    store.mark_rejected(record.confirmation_id)
    writer = FakeNotionWriter()
    service = _build_service(store, writer)

    with pytest.raises(ValueError):
        asyncio.run(service.finalize_approval(record.confirmation_id))

    assert writer.calls == []


def _build_store(tmp_path: Path) -> ConfirmationStore:
    path = tmp_path / "confirmations.db"
    return ConfirmationStore(f"sqlite:///{path}")


def _build_service(
    store: ConfirmationStore,
    writer: FakeNotionWriter,
) -> IntakeService:
    service = IntakeService.__new__(IntakeService)
    service.store = store
    service.notion_writer = writer
    return service


def _build_confirmation() -> ConfirmationRecord:
    now = utc_now()
    return ConfirmationRecord(
        confirmation_id="confirmation-id",
        user_id="user-id",
        chat_id="chat-id",
        source_message_id="message-id",
        session_id="session-id",
        invocation_id="invocation-id",
        request_input_call_id="call-id",
        status=ConfirmationStatus.PENDING_REVIEW,
        incoming_context=IncomingContext(raw_text="https://example.com"),
        draft_proposal=ProjectDraft(
            project_name="Example project",
            summary="A small example project.",
            resource_type="article",
            intent="explore",
            priority="Medium",
            source_url="https://example.com",
            tasks=["Review example"],
        ),
        review_message="Review this draft.",
        created_at=now,
        updated_at=now,
    )


def test_duplicate_source_is_reported_instead_of_creating_a_second_project(
    tmp_path: Path,
):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter()
    writer.existing_project = ExistingProject(
        page_id="existing-id",
        page_url="https://notion.so/existing-id",
        created_time="2026-06-04T09:12:00.000Z",
    )
    service = _build_service(store, writer)

    result = asyncio.run(service.finalize_approval(record.confirmation_id))

    assert result.status == ConfirmationStatus.DUPLICATE.value
    assert result.notion_project_url == "https://notion.so/existing-id"
    assert result.duplicate_created_time == "2026-06-04T09:12:00.000Z"
    assert writer.calls == []

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.status == ConfirmationStatus.DUPLICATE
    assert stored.notion_project_id == "existing-id"


def test_a_duplicate_can_have_its_tasks_added_to_the_existing_project(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter()
    writer.existing_project = ExistingProject(
        page_id="existing-id",
        page_url="https://notion.so/existing-id",
    )
    service = _build_service(store, writer)

    asyncio.run(service.finalize_approval(record.confirmation_id))
    result = asyncio.run(service.add_to_existing_project(record.confirmation_id))

    assert result.status == ConfirmationStatus.COMMITTED.value
    assert writer.added_task_project_ids == ["existing-id"]
    assert writer.calls == []

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.notion_task_ids == ["new-task-id"]


def test_undo_archives_every_page_the_commit_created(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter()
    service = _build_service(store, writer)

    asyncio.run(service.finalize_approval(record.confirmation_id))
    result = asyncio.run(service.undo_commit(record.confirmation_id))

    assert result.status == ConfirmationStatus.UNDONE.value
    assert writer.archived == ["project-id", "task-id"]

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.status == ConfirmationStatus.UNDONE
    assert stored.notion_project_url is None
    assert stored.notion_task_ids == []


def test_undo_is_a_no_op_when_nothing_was_committed(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    writer = FakeNotionWriter()
    service = _build_service(store, writer)

    result = asyncio.run(service.undo_commit(record.confirmation_id))

    assert result.status == ConfirmationStatus.PENDING_REVIEW.value
    assert writer.archived == []

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft
from backlog_tamer.application.confirmation_store import ConfirmationStore, utc_now
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationRecord, ConfirmationStatus
from backlog_tamer.integrations.notion import NotionCommitResult


class FakeNotionWriter:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[ProjectDraft] = []

    async def create_project_with_tasks(
        self,
        draft: ProjectDraft,
    ) -> NotionCommitResult:
        self.calls.append(draft)
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

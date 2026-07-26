from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft


class ConfirmationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    COMMITTING = "committing"
    COMMITTED = "committed"
    REJECTED = "rejected"
    FAILED = "failed"


class ConfirmationRecord(BaseModel):
    confirmation_id: str
    user_id: str
    chat_id: str | None = None
    source_message_id: str | None = None
    session_id: str
    invocation_id: str
    request_input_call_id: str
    status: ConfirmationStatus
    incoming_context: IncomingContext
    draft_proposal: ProjectDraft
    review_message: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    notion_project_id: str | None = None
    notion_project_url: str | None = None
    failure_reason: str | None = None


class IntakeResult(BaseModel):
    status: str
    confirmation_id: str | None = None
    draft_proposal: ProjectDraft | None = None
    review_message: str | None = None
    notion_project_url: str | None = None
    failure_reason: str | None = None

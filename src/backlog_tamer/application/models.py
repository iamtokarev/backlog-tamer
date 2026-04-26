from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from backlog_tamer.agents.intake_triage.schemas import DraftProposal, IncomingContext


class ConfirmationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


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
    draft_proposal: DraftProposal
    review_message: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class IntakeResult(BaseModel):
    status: str
    confirmation_id: str | None = None
    draft_proposal: DraftProposal | None = None
    review_message: str | None = None

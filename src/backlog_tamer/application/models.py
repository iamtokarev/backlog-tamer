from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, field_validator

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)


class ConfirmationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    COMMITTING = "committing"
    COMMITTED = "committed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNDONE = "undone"
    DUPLICATE = "duplicate"


class ManualEdit(BaseModel):
    """One field the user corrected, and what the agent had proposed.

    Storing only the new value made every correction invisible: a week of
    traces read as a clean 7/7 approval rate while fields were being fixed by
    hand. The pair is the per-field accuracy signal that revision counts can
    no longer provide.
    """

    before: str = ""
    after: str

    @classmethod
    def coerce(cls, value: object) -> ManualEdit:
        # Rows written before `before` existed stored the new value alone.
        if isinstance(value, str):
            return cls(after=value)
        if isinstance(value, ManualEdit):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        raise TypeError(f"Cannot read a manual edit from {type(value)!r}.")


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
    grounding: DraftGrounding = DraftGrounding()
    # Fields the user changed with the inline buttons since the agent last
    # drafted. Replayed into the next revision so the agent does not undo them.
    manual_edits: dict[str, ManualEdit] = {}
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    notion_project_id: str | None = None
    notion_project_url: str | None = None
    notion_task_ids: list[str] = []
    failure_reason: str | None = None

    @field_validator("manual_edits", mode="before")
    @classmethod
    def _read_legacy_manual_edits(cls, value: object) -> object:
        """Accept rows written when an edit was just its new value."""
        if not isinstance(value, dict):
            return value
        return {field: ManualEdit.coerce(edit) for field, edit in value.items()}

    @property
    def corrected_fields(self) -> dict[str, ManualEdit]:
        """Edits where the user actually moved the agent off its answer."""
        return {
            field: edit
            for field, edit in self.manual_edits.items()
            if edit.before and edit.before != edit.after
        }


class IntakeResult(BaseModel):
    status: str
    confirmation_id: str | None = None
    draft_proposal: ProjectDraft | None = None
    grounding: DraftGrounding = DraftGrounding()
    review_message: str | None = None
    notion_project_url: str | None = None
    duplicate_created_time: str | None = None
    failure_reason: str | None = None

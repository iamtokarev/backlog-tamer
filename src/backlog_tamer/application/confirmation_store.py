from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)

from .database_urls import to_sync_database_url, uses_external_pooler
from .models import ConfirmationRecord, ConfirmationStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConfirmationRow(Base):
    __tablename__ = "confirmations"

    confirmation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_input_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    incoming_context_json: Mapped[str] = mapped_column(Text, nullable=False)
    draft_proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    review_message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    notion_project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notion_project_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_edits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    grounding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notion_task_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfirmationStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(
            to_sync_database_url(database_url),
            poolclass=NullPool if uses_external_pooler(database_url) else None,
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._ensure_commit_columns()

    def create_pending(self, record: ConfirmationRecord) -> None:
        with self.session_factory.begin() as session:
            session.add(self._row_from_record(record))

    def get(self, confirmation_id: str) -> ConfirmationRecord | None:
        with self.session_factory() as session:
            row = session.get(ConfirmationRow, confirmation_id)
            return self._record_from_row(row) if row is not None else None

    def update_after_resume(
        self,
        *,
        confirmation_id: str,
        draft_proposal: ProjectDraft,
        invocation_id: str,
        request_input_call_id: str,
        review_message: str,
        grounding: DraftGrounding | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.draft_proposal_json = draft_proposal.model_dump_json()
            if grounding is not None:
                row.grounding_json = grounding.model_dump_json()
            row.invocation_id = invocation_id
            row.request_input_call_id = request_input_call_id
            row.review_message = review_message
            row.manual_edits_json = None
            row.updated_at = utc_now()

    def apply_manual_edit(
        self,
        *,
        confirmation_id: str,
        field: str,
        value: str,
    ) -> ConfirmationRecord:
        """Patch one draft field in place, without re-running the agent."""
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            if row.status != ConfirmationStatus.PENDING_REVIEW.value:
                raise ValueError(
                    f"Confirmation {confirmation_id} is no longer editable: "
                    f"{row.status}."
                )

            draft = ProjectDraft.model_validate_json(row.draft_proposal_json)
            updated = draft.model_copy(update={field: value})
            row.draft_proposal_json = updated.model_dump_json()
            row.manual_edits_json = json.dumps(
                {**_load_manual_edits(row.manual_edits_json), field: value}
            )
            row.updated_at = utc_now()
            return self._record_from_row(row)

    def mark_committing_once(
        self,
        confirmation_id: str,
    ) -> tuple[ConfirmationRecord, bool]:
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            # FAILED is retryable: the draft is intact, only the Notion write lost.
            if row.status in {
                ConfirmationStatus.PENDING_REVIEW.value,
                ConfirmationStatus.FAILED.value,
            }:
                row.status = ConfirmationStatus.COMMITTING.value
                row.updated_at = now
                row.failure_reason = None
                return self._record_from_row(row), True
            if row.status in {
                ConfirmationStatus.COMMITTING.value,
                ConfirmationStatus.COMMITTED.value,
            }:
                return self._record_from_row(row), False
            raise ValueError(
                "Confirmation "
                f"{confirmation_id} cannot be committed from status {row.status}."
            )

    def mark_committed(
        self,
        *,
        confirmation_id: str,
        notion_project_id: str,
        notion_project_url: str,
        notion_task_ids: list[str] | None = None,
    ) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.status = ConfirmationStatus.COMMITTED.value
            row.notion_project_id = notion_project_id
            row.notion_project_url = notion_project_url
            row.notion_task_ids_json = json.dumps(notion_task_ids or [])
            row.failure_reason = None
            row.updated_at = now
            row.resolved_at = now

    def mark_duplicate(
        self,
        *,
        confirmation_id: str,
        notion_project_id: str,
        notion_project_url: str,
    ) -> None:
        """Point the record at the project this URL already has."""
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.status = ConfirmationStatus.DUPLICATE.value
            row.notion_project_id = notion_project_id
            row.notion_project_url = notion_project_url
            row.failure_reason = None
            row.updated_at = now

    def mark_undone(self, confirmation_id: str) -> ConfirmationRecord:
        """The pages were archived, so the record no longer points at Notion."""
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            if row.status != ConfirmationStatus.COMMITTED.value:
                raise ValueError(
                    f"Confirmation {confirmation_id} is not committed: {row.status}."
                )
            row.status = ConfirmationStatus.UNDONE.value
            row.notion_project_id = None
            row.notion_project_url = None
            row.notion_task_ids_json = None
            row.updated_at = now
            row.resolved_at = now
            return self._record_from_row(row)

    def mark_failed(self, *, confirmation_id: str, failure_reason: str) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.status = ConfirmationStatus.FAILED.value
            row.failure_reason = failure_reason
            row.updated_at = now

    def mark_rejected(self, confirmation_id: str) -> None:
        self._mark_completed(confirmation_id, ConfirmationStatus.REJECTED)

    def _mark_completed(
        self,
        confirmation_id: str,
        status: ConfirmationStatus,
    ) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.status = status.value
            row.updated_at = now
            row.resolved_at = now

    def _get_required_row(
        self,
        session: Session,
        confirmation_id: str,
    ) -> ConfirmationRow:
        row = session.get(ConfirmationRow, confirmation_id)
        if row is None:
            raise ValueError(f"Unknown confirmation_id: {confirmation_id}")
        return row

    def _row_from_record(self, record: ConfirmationRecord) -> ConfirmationRow:
        return ConfirmationRow(
            confirmation_id=record.confirmation_id,
            user_id=record.user_id,
            chat_id=record.chat_id,
            source_message_id=record.source_message_id,
            session_id=record.session_id,
            invocation_id=record.invocation_id,
            request_input_call_id=record.request_input_call_id,
            status=record.status.value,
            incoming_context_json=record.incoming_context.model_dump_json(),
            draft_proposal_json=record.draft_proposal.model_dump_json(),
            review_message=record.review_message,
            created_at=record.created_at,
            updated_at=record.updated_at,
            resolved_at=record.resolved_at,
            notion_project_id=record.notion_project_id,
            notion_project_url=record.notion_project_url,
            failure_reason=record.failure_reason,
            manual_edits_json=json.dumps(record.manual_edits)
            if record.manual_edits
            else None,
            grounding_json=record.grounding.model_dump_json(),
            notion_task_ids_json=json.dumps(record.notion_task_ids)
            if record.notion_task_ids
            else None,
        )

    def _record_from_row(self, row: ConfirmationRow) -> ConfirmationRecord:
        return ConfirmationRecord(
            confirmation_id=row.confirmation_id,
            user_id=row.user_id,
            chat_id=row.chat_id,
            source_message_id=row.source_message_id,
            session_id=row.session_id,
            invocation_id=row.invocation_id,
            request_input_call_id=row.request_input_call_id,
            status=ConfirmationStatus(row.status),
            incoming_context=IncomingContext.model_validate_json(
                row.incoming_context_json
            ),
            draft_proposal=ProjectDraft.model_validate_json(row.draft_proposal_json),
            review_message=row.review_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
            notion_project_id=row.notion_project_id,
            notion_project_url=row.notion_project_url,
            failure_reason=row.failure_reason,
            manual_edits=_load_manual_edits(row.manual_edits_json),
            grounding=_load_grounding(row.grounding_json),
            notion_task_ids=_load_task_ids(row.notion_task_ids_json),
        )

    def _ensure_commit_columns(self) -> None:
        inspector = inspect(self.engine)
        columns = {column["name"] for column in inspector.get_columns("confirmations")}
        additions = {
            "notion_project_id": "VARCHAR(255)",
            "notion_project_url": "TEXT",
            "failure_reason": "TEXT",
            "manual_edits_json": "TEXT",
            "grounding_json": "TEXT",
            "notion_task_ids_json": "TEXT",
        }
        missing = [
            (column_name, column_type)
            for column_name, column_type in additions.items()
            if column_name not in columns
        ]
        if not missing:
            return

        with self.engine.begin() as connection:
            for column_name, column_type in missing:
                connection.execute(
                    text(
                        "ALTER TABLE confirmations "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
                )


def _load_task_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _load_grounding(raw: str | None) -> DraftGrounding:
    if not raw:
        return DraftGrounding()
    try:
        return DraftGrounding.model_validate_json(raw)
    except ValueError:
        return DraftGrounding()


def _load_manual_edits(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backlog_tamer.agents.intake_triage.schemas import DraftProposal, IncomingContext

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


class ConfirmationStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

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
        draft_proposal: DraftProposal,
        invocation_id: str,
        request_input_call_id: str,
        review_message: str,
    ) -> None:
        with self.session_factory.begin() as session:
            row = self._get_required_row(session, confirmation_id)
            row.draft_proposal_json = draft_proposal.model_dump_json()
            row.invocation_id = invocation_id
            row.request_input_call_id = request_input_call_id
            row.review_message = review_message
            row.updated_at = utc_now()

    def mark_approved(self, confirmation_id: str) -> None:
        self._mark_completed(confirmation_id, ConfirmationStatus.APPROVED)

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
            draft_proposal=DraftProposal.model_validate_json(row.draft_proposal_json),
            review_message=row.review_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
        )

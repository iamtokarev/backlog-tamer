from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import BigInteger, DateTime, String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from backlog_tamer.database_urls import (
    to_sync_database_url,
    uses_external_pooler,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class PendingRevision(NamedTuple):
    """A Revision the user asked for and has not sent the note for yet."""

    confirmation_id: str
    # The review card whose keyboard became "Cancel revision"; retired once the
    # note arrives. None for a wait recorded before this column existed.
    card_message_id: int | None = None


class TelegramRevisionRow(Base):
    __tablename__ = "telegram_revision_states"

    state_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class TelegramUpdateRow(Base):
    __tablename__ = "telegram_processed_updates"

    update_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class TelegramStateStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(
            to_sync_database_url(database_url),
            poolclass=NullPool if uses_external_pooler(database_url) else None,
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._ensure_revision_columns()

    def set_awaiting_revision(
        self,
        *,
        user_id: str,
        chat_id: str,
        confirmation_id: str,
        card_message_id: int | None = None,
    ) -> None:
        key = self._state_key(user_id, chat_id)
        now = utc_now()
        with self.session_factory.begin() as session:
            row = session.get(TelegramRevisionRow, key)
            if row is None:
                session.add(
                    TelegramRevisionRow(
                        state_key=key,
                        user_id=user_id,
                        chat_id=chat_id,
                        confirmation_id=confirmation_id,
                        card_message_id=card_message_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            row.confirmation_id = confirmation_id
            row.card_message_id = card_message_id
            row.updated_at = now

    def pop_awaiting_revision(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> PendingRevision | None:
        key = self._state_key(user_id, chat_id)
        with self.session_factory.begin() as session:
            row = session.get(TelegramRevisionRow, key)
            if row is None:
                return None
            pending = PendingRevision(row.confirmation_id, row.card_message_id)
            session.delete(row)
            return pending

    def record_update_once(self, update_id: int | str) -> bool:
        now = utc_now()
        update_key = str(update_id)
        try:
            with self.session_factory.begin() as session:
                session.add(
                    TelegramUpdateRow(
                        update_id=update_key,
                        created_at=now,
                    )
                )
        except IntegrityError:
            return False
        return True

    def has_processed_update(self, update_id: int | str) -> bool:
        with self.session_factory() as session:
            return session.get(TelegramUpdateRow, str(update_id)) is not None

    @staticmethod
    def _state_key(user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    def _ensure_revision_columns(self) -> None:
        # create_all leaves an existing table alone, so a column added since
        # the table was first created has to be added here.
        inspector = inspect(self.engine)
        columns = {
            column["name"]
            for column in inspector.get_columns(TelegramRevisionRow.__tablename__)
        }
        if "card_message_id" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE telegram_revision_states "
                    "ADD COLUMN card_message_id BIGINT"
                )
            )


def state_identity_from_update(update) -> tuple[str, str] | None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return None
    return str(user.id), str(chat.id)


def get_session_revision(
    context,
    *,
    user_id: str,
    chat_id: str,
) -> PendingRevision | None:
    store = context.bot_data.get("telegram_state_store")
    if store is not None:
        return store.pop_awaiting_revision(user_id=user_id, chat_id=chat_id)
    return context.user_data.pop("awaiting_revision_for", None)


def set_session_revision(
    context,
    *,
    user_id: str,
    chat_id: str,
    confirmation_id: str,
    card_message_id: int | None = None,
) -> None:
    store = context.bot_data.get("telegram_state_store")
    if store is not None:
        store.set_awaiting_revision(
            user_id=user_id,
            chat_id=chat_id,
            confirmation_id=confirmation_id,
            card_message_id=card_message_id,
        )
        return
    context.user_data["awaiting_revision_for"] = PendingRevision(
        confirmation_id,
        card_message_id,
    )

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TelegramRevisionRow(Base):
    __tablename__ = "telegram_revision_states"

    state_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
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
        self.engine = create_engine(database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def set_awaiting_revision(
        self,
        *,
        user_id: str,
        chat_id: str,
        confirmation_id: str,
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
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            row.confirmation_id = confirmation_id
            row.updated_at = now

    def pop_awaiting_revision(self, *, user_id: str, chat_id: str) -> str | None:
        key = self._state_key(user_id, chat_id)
        with self.session_factory.begin() as session:
            row = session.get(TelegramRevisionRow, key)
            if row is None:
                return None
            confirmation_id = row.confirmation_id
            session.delete(row)
            return confirmation_id

    def record_update_once(self, update_id: int | str) -> bool:
        now = utc_now()
        update_key = str(update_id)
        with self.session_factory.begin() as session:
            inserted = session.execute(
                text(
                    "INSERT OR IGNORE INTO telegram_processed_updates "
                    "(update_id, created_at) VALUES (:update_id, :created_at)"
                ),
                {"update_id": update_key, "created_at": now},
            )
            return inserted.rowcount == 1

    def has_processed_update(self, update_id: int | str) -> bool:
        with self.session_factory() as session:
            return session.get(TelegramUpdateRow, str(update_id)) is not None

    @staticmethod
    def _state_key(user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"


def state_identity_from_update(update) -> tuple[str, str] | None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return None
    return str(user.id), str(chat.id)


def get_session_revision(context, *, user_id: str, chat_id: str) -> str | None:
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
) -> None:
    store = context.bot_data.get("telegram_state_store")
    if store is not None:
        store.set_awaiting_revision(
            user_id=user_id,
            chat_id=chat_id,
            confirmation_id=confirmation_id,
        )
        return
    context.user_data["awaiting_revision_for"] = confirmation_id

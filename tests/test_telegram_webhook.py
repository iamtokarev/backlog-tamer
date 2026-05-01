from __future__ import annotations

from pathlib import Path

from backlog_tamer.integrations.telegram.state import TelegramStateStore
from backlog_tamer.integrations.telegram.webhook import (
    TELEGRAM_SECRET_HEADER,
    decode_json_body,
    validate_webhook_update,
)


def test_validate_webhook_accepts_allowed_message_update():
    payload = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "is_bot": False, "first_name": "User"},
            "chat": {"id": 42, "type": "private"},
            "date": 1,
            "text": "https://example.com",
        },
    }

    result = validate_webhook_update(
        payload=payload,
        headers={TELEGRAM_SECRET_HEADER: "secret"},
        expected_secret="secret",
        allowed_user_id=42,
    )

    assert result.accepted is True
    assert result.reason is None


def test_validate_webhook_rejects_wrong_secret():
    payload = {
        "update_id": 123,
        "message": {
            "from": {"id": 42},
        },
    }

    result = validate_webhook_update(
        payload=payload,
        headers={TELEGRAM_SECRET_HEADER: "wrong"},
        expected_secret="secret",
        allowed_user_id=42,
    )

    assert result.accepted is False
    assert result.reason == "invalid_secret"


def test_validate_webhook_ignores_unauthorized_user():
    payload = {
        "update_id": 123,
        "callback_query": {
            "id": "callback-id",
            "from": {"id": 7},
            "data": "approve:confirmation-id",
        },
    }

    result = validate_webhook_update(
        payload=payload,
        headers={},
        expected_secret=None,
        allowed_user_id=42,
    )

    assert result.accepted is False
    assert result.reason == "unauthorized_user"


def test_decode_json_body_requires_object():
    assert decode_json_body(b'{"update_id": 123}') == {"update_id": 123}


def test_telegram_state_store_persists_revision_and_dedupes_updates(tmp_path: Path):
    store = TelegramStateStore(f"sqlite:///{tmp_path / 'telegram.db'}")

    store.set_awaiting_revision(
        user_id="42",
        chat_id="99",
        confirmation_id="confirmation-id",
    )

    assert store.pop_awaiting_revision(user_id="42", chat_id="99") == "confirmation-id"
    assert store.pop_awaiting_revision(user_id="42", chat_id="99") is None

    assert store.record_update_once(123) is True
    assert store.record_update_once(123) is False
    assert store.has_processed_update(123) is True

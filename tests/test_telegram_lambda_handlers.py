from __future__ import annotations

import json
from types import SimpleNamespace

from backlog_tamer.integrations.telegram.lambda_handlers import (
    QUEUE_URL_ENV,
    SECRET_ARN_ENV,
    webhook_handler,
)


class FakeSQSClient:
    def __init__(self):
        self.sent_messages: list[dict] = []

    def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return {"MessageId": "message-id"}


def test_webhook_handler_validates_and_enqueues(
    monkeypatch,
):
    monkeypatch.delenv(SECRET_ARN_ENV, raising=False)
    monkeypatch.setattr(
        "backlog_tamer.integrations.telegram.lambda_handlers._SECRETS_LOADED",
        False,
    )
    sqs = FakeSQSClient()
    monkeypatch.setenv(QUEUE_URL_ENV, "https://sqs.example/queue")
    monkeypatch.setenv("TELEGRAM__ALLOWED_USER_ID", "42")
    monkeypatch.setenv("TELEGRAM__WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(
        "backlog_tamer.integrations.telegram.lambda_handlers._aws_client",
        lambda service_name: sqs,
    )

    event = _lambda_event(
        {
            "update_id": 123,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "chat": {"id": 42, "type": "private"},
                "date": 1,
                "text": "https://example.com",
            },
        },
        secret="secret",
    )

    result = webhook_handler(event, SimpleNamespace())

    assert result["statusCode"] == 200
    assert result["body"] == "ok"
    assert len(sqs.sent_messages) == 1
    assert sqs.sent_messages[0]["QueueUrl"] == "https://sqs.example/queue"
    assert json.loads(sqs.sent_messages[0]["MessageBody"])["update_id"] == 123


def test_webhook_handler_rejects_invalid_secret(monkeypatch):
    monkeypatch.delenv(SECRET_ARN_ENV, raising=False)
    monkeypatch.setattr(
        "backlog_tamer.integrations.telegram.lambda_handlers._SECRETS_LOADED",
        False,
    )
    monkeypatch.setenv("TELEGRAM__ALLOWED_USER_ID", "42")
    monkeypatch.setenv("TELEGRAM__WEBHOOK_SECRET", "secret")

    event = _lambda_event(
        {
            "update_id": 123,
            "message": {
                "from": {"id": 42},
            },
        },
        secret="wrong",
    )

    result = webhook_handler(event, SimpleNamespace())

    assert result["statusCode"] == 403
    assert result["body"] == "forbidden"


def _lambda_event(payload: dict, *, secret: str) -> dict:
    return {
        "headers": {"x-telegram-bot-api-secret-token": secret},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }

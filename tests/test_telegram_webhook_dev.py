from __future__ import annotations

import json
import queue
import threading
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from backlog_tamer.integrations.telegram.webhook import TELEGRAM_SECRET_HEADER
from backlog_tamer.integrations.telegram.webhook_dev import (
    MAX_WEBHOOK_BODY_BYTES,
    _build_server,
)


class FakeStateStore:
    def record_update_once(self, _update_id: int) -> bool:
        return True


@pytest.mark.parametrize("configured_secret", [None, SecretStr(""), SecretStr("   ")])
def test_webhook_server_refuses_to_start_without_a_secret(configured_secret):
    settings = SimpleNamespace(
        telegram=SimpleNamespace(
            webhook_secret=configured_secret,
            allowed_user_id=42,
        )
    )

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET must be configured"):
        _build_server(
            host="127.0.0.1",
            port=0,
            path="/telegram/webhook",
            settings=settings,
            state_store=FakeStateStore(),
            updates=queue.Queue(),
        )


@pytest.fixture
def webhook_server():
    settings = SimpleNamespace(
        telegram=SimpleNamespace(
            webhook_secret=SecretStr("secret"),
            allowed_user_id=42,
        )
    )
    updates: queue.Queue[dict | None] = queue.Queue()
    server = _build_server(
        host="127.0.0.1",
        port=0,
        path="/telegram/webhook",
        settings=settings,
        state_store=FakeStateStore(),
        updates=updates,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, updates
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_webhook_rejects_unauthenticated_request_before_reading_body(webhook_server):
    server, updates = webhook_server

    status = _headers_only_request(
        server.server_port,
        {"content-length": str(MAX_WEBHOOK_BODY_BYTES + 1)},
    )

    assert status == 403
    assert updates.empty()


@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [
        (None, 411),
        ("not-a-number", 400),
        ("-1", 400),
        (str(MAX_WEBHOOK_BODY_BYTES + 1), 413),
    ],
)
def test_webhook_rejects_invalid_length_without_reading_body(
    webhook_server,
    content_length,
    expected_status,
):
    server, updates = webhook_server
    headers = {TELEGRAM_SECRET_HEADER: "secret"}
    if content_length is not None:
        headers["content-length"] = content_length

    status = _headers_only_request(server.server_port, headers)

    assert status == expected_status
    assert updates.empty()


def test_webhook_accepts_valid_authenticated_update(webhook_server):
    server, updates = webhook_server
    payload = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "from": {"id": 42},
        },
    }
    body = json.dumps(payload).encode()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=1)
    connection.request(
        "POST",
        "/telegram/webhook",
        body=body,
        headers={TELEGRAM_SECRET_HEADER: "secret"},
    )
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == 200
    assert updates.get_nowait() == payload


def _headers_only_request(port: int, headers: dict[str, str]) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
    connection.putrequest("POST", "/telegram/webhook")
    for name, value in headers.items():
        connection.putheader(name, value)
    connection.endheaders()
    try:
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()

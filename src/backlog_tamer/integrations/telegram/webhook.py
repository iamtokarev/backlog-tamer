from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram.ext import Application

TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"


@dataclass(frozen=True)
class WebhookValidationResult:
    accepted: bool
    reason: str | None = None


class TelegramUpdateProcessor:
    def __init__(self, application: "Application"):
        self.application = application
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self.application.initialize()
        await self.application.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self.application.stop()
        await self.application.shutdown()
        self._started = False

    async def process_update_payload(self, payload: dict[str, Any]) -> None:
        from telegram import Update

        if not self._started:
            await self.start()
        update = Update.de_json(payload, self.application.bot)
        self._insert_callback_data(update)
        await self.application.process_update(update)

    async def enqueue_update_payload(self, payload: dict[str, Any]) -> None:
        from telegram import Update

        if not self._started:
            await self.start()
        update = Update.de_json(payload, self.application.bot)
        self._insert_callback_data(update)
        await self.application.update_queue.put(update)

    def _insert_callback_data(self, update: Update) -> None:
        if getattr(self.application.bot, "callback_data_cache", None) is None:
            return
        self.application.bot.insert_callback_data(update)


def decode_json_body(body: str | bytes | None) -> dict[str, Any]:
    if body is None:
        raise ValueError("Missing request body.")
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Telegram update body must be a JSON object.")
    return payload


def validate_webhook_update(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    expected_secret: str | None,
    allowed_user_id: int,
) -> WebhookValidationResult:
    if expected_secret:
        actual_secret = _header_value(headers, TELEGRAM_SECRET_HEADER)
        if actual_secret is None or not hmac.compare_digest(
            actual_secret,
            expected_secret,
        ):
            return WebhookValidationResult(False, "invalid_secret")

    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        return WebhookValidationResult(False, "missing_update_id")

    user_id = _telegram_user_id(payload)
    if user_id != allowed_user_id:
        return WebhookValidationResult(False, "unauthorized_user")

    if not _is_supported_update(payload):
        return WebhookValidationResult(False, "unsupported_update")

    return WebhookValidationResult(True)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _telegram_user_id(payload: dict[str, Any]) -> int | None:
    message = payload.get("message")
    if isinstance(message, dict):
        sender = message.get("from")
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            return sender["id"]

    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from")
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            return sender["id"]

    return None


def _is_supported_update(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("message"), dict) or isinstance(
        payload.get("callback_query"),
        dict,
    )

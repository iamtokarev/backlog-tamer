from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

from .webhook import (
    decode_json_body,
    validate_webhook_update,
)

logger = logging.getLogger(__name__)

QUEUE_URL_ENV = "TELEGRAM_UPDATES_QUEUE_URL"
SECRET_ARN_ENV = "BACKLOG_TAMER_SECRET_ARN"
_SECRETS_LOADED = False


def webhook_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    _load_runtime_secrets()
    config = _telegram_webhook_config()

    try:
        payload = decode_json_body(_event_body(event))
    except Exception:
        logger.exception("Invalid Telegram webhook payload")
        return _response(400, "invalid json")

    validation = validate_webhook_update(
        payload=payload,
        headers=event.get("headers") or {},
        expected_secret=config["webhook_secret"],
        allowed_user_id=config["allowed_user_id"],
    )
    if not validation.accepted:
        if validation.reason == "invalid_secret":
            return _response(403, "forbidden")
        logger.info("Ignoring Telegram update: %s", validation.reason)
        return _response(200, "ignored")

    update_id = payload["update_id"]
    queue_url = os.environ[QUEUE_URL_ENV]
    _aws_client("sqs").send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload, separators=(",", ":")),
        MessageAttributes={
            "telegram_update_id": {
                "DataType": "String",
                "StringValue": str(update_id),
            }
        },
    )
    return _response(200, "ok")


def worker_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    _load_runtime_secrets()
    if event.get("healthcheck"):
        return _healthcheck()
    return asyncio.run(_process_sqs_event(event))


def _healthcheck() -> dict[str, Any]:
    """Prove the deployed image can do real work, and report its version.

    Invoked by the deploy workflow. The imports are deliberately eager: the
    heavy agent and Notion modules are otherwise only loaded when a real update
    arrives, so a broken image would first surface as a failed user message.
    """
    from importlib.metadata import version

    from backlog_tamer.agents.intake_triage import agent, workflow  # noqa: F401
    from backlog_tamer.agents.intake_triage.tools import fetch_url
    from backlog_tamer.integrations.notion import writer  # noqa: F401

    _get_settings()

    missing = fetch_url.missing_optional_dependencies()
    if missing:
        raise RuntimeError(
            f"Deployed image is missing extraction dependencies: {', '.join(missing)}"
        )

    # importlib.metadata returns None rather than raising when the installed
    # dist-info is present but incomplete, which would let the deploy's version
    # assertion fail with a confusing message instead of this one.
    installed_version = version("backlog-tamer")
    if not installed_version:
        raise RuntimeError("Could not determine the installed backlog-tamer version.")

    return {"ok": True, "version": installed_version}


async def _process_sqs_event(event: dict[str, Any]) -> dict[str, Any]:
    from .state import TelegramStateStore

    settings = _get_settings()
    state_store = TelegramStateStore(settings.database_url)
    application = _build_worker_application(settings, state_store)
    processor = _build_update_processor(application)
    failures: list[dict[str, str]] = []

    await processor.start()
    try:
        for record in event.get("Records", []):
            message_id = record.get("messageId", "")
            try:
                payload = json.loads(record["body"])
                if not isinstance(payload, dict):
                    raise ValueError("SQS record body must contain a JSON object.")
                update_id = payload.get("update_id")
                if isinstance(update_id, int) and not state_store.record_update_once(
                    update_id,
                ):
                    logger.info("Ignoring duplicate Telegram update_id=%s", update_id)
                    continue
                await processor.process_update_payload(payload)
            except Exception:
                logger.exception("Failed to process SQS message_id=%s", message_id)
                failures.append({"itemIdentifier": message_id})
    finally:
        await processor.stop()

    return {"batchItemFailures": failures}


def _event_body(event: dict[str, Any]) -> str | bytes | None:
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body


def _response(status_code: int, body: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "text/plain; charset=utf-8"},
        "body": body,
    }


def _load_runtime_secrets() -> None:
    global _SECRETS_LOADED

    if _SECRETS_LOADED:
        return

    secret_arn = os.environ.get(SECRET_ARN_ENV)
    if not secret_arn:
        _SECRETS_LOADED = True
        return

    response = _aws_client("secretsmanager").get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError(f"Secret {secret_arn} does not contain SecretString.")

    values = json.loads(secret_string)
    if not isinstance(values, dict):
        raise RuntimeError(f"Secret {secret_arn} must contain a JSON object.")

    for key, value in values.items():
        if value is None:
            continue
        os.environ.setdefault(str(key), str(value))

    _clear_settings_cache()

    _SECRETS_LOADED = True


def _build_worker_application(settings, state_store):
    from backlog_tamer.application.intake_service import IntakeService

    from .bot import build_application

    return build_application(
        settings,
        IntakeService(settings=settings),
        state_store=state_store,
    )


def _build_update_processor(application):
    from .webhook import TelegramUpdateProcessor

    return TelegramUpdateProcessor(application)


def _aws_client(service_name: str):
    import boto3

    return boto3.client(service_name)


def _get_settings():
    from backlog_tamer.config import get_settings

    return get_settings()


def _clear_settings_cache() -> None:
    config_module = sys.modules.get("backlog_tamer.config")
    if config_module is None:
        return

    config_module.get_settings.cache_clear()


def _telegram_webhook_config() -> dict[str, Any]:
    allowed_user_id = os.environ.get("TELEGRAM__ALLOWED_USER_ID")
    if allowed_user_id is None:
        raise RuntimeError("TELEGRAM__ALLOWED_USER_ID is required.")

    return {
        "allowed_user_id": int(allowed_user_id),
        "webhook_secret": os.environ.get("TELEGRAM__WEBHOOK_SECRET"),
    }

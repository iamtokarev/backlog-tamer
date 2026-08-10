from __future__ import annotations

import argparse
import asyncio
import logging
import queue
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.config import get_settings

from .bot import build_application
from .state import TelegramStateStore
from .webhook import (
    TelegramUpdateProcessor,
    decode_json_body,
    validate_webhook_secret,
    validate_webhook_update,
)

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 1_000_000
WEBHOOK_READ_TIMEOUT_SECONDS = 10.0


class _TimeoutThreadingHTTPServer(ThreadingHTTPServer):
    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(WEBHOOK_READ_TIMEOUT_SECONDS)
        return request, client_address


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Telegram webhook.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/telegram/webhook")
    parser.add_argument(
        "--public-url",
        help="Optional HTTPS tunnel origin used to register Telegram setWebhook.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    state_store = TelegramStateStore(settings.database_url)
    updates: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stop_event = threading.Event()

    application = build_application(
        settings,
        IntakeService(settings=settings),
        state_store=state_store,
    )
    processor = TelegramUpdateProcessor(application)
    worker = threading.Thread(
        target=_run_worker,
        args=(processor, updates, stop_event, args.public_url, args.path),
        daemon=True,
    )
    worker.start()

    server = _build_server(
        host=args.host,
        port=args.port,
        path=args.path,
        settings=settings,
        state_store=state_store,
        updates=updates,
    )

    def stop_server(_signum, _frame) -> None:
        logger.info("Stopping local webhook server")
        stop_event.set()
        updates.put(None)
        server.shutdown()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)

    logger.info(
        "Local Telegram webhook listening on http://%s:%s%s",
        args.host,
        args.port,
        args.path,
    )
    if args.public_url is None:
        logger.info("Expose this server via HTTPS tunnel and register setWebhook.")

    server.serve_forever()
    worker.join(timeout=10)


def _run_worker(
    processor: TelegramUpdateProcessor,
    updates: queue.Queue[dict[str, Any] | None],
    stop_event: threading.Event,
    public_url: str | None,
    path: str,
) -> None:
    asyncio.run(_worker_loop(processor, updates, stop_event, public_url, path))


async def _worker_loop(
    processor: TelegramUpdateProcessor,
    updates: queue.Queue[dict[str, Any] | None],
    stop_event: threading.Event,
    public_url: str | None,
    path: str,
) -> None:
    await processor.start()
    try:
        if public_url is not None:
            url = f"{public_url.rstrip('/')}{path}"
            secret = processor.application.bot_data.get("telegram_webhook_secret")
            webhook_kwargs = {}
            if secret is not None:
                webhook_kwargs["secret_token"] = secret
            await processor.application.bot.set_webhook(
                url=url,
                allowed_updates=["message", "callback_query"],
                **webhook_kwargs,
            )
            logger.info("Registered Telegram webhook: %s", url)

        while not stop_event.is_set():
            payload = await asyncio.to_thread(updates.get)
            if payload is None:
                return
            try:
                await processor.process_update_payload(payload)
            except Exception:
                logger.exception("Failed to process Telegram update")
            finally:
                updates.task_done()
    finally:
        await processor.stop()


def _build_server(
    *,
    host: str,
    port: int,
    path: str,
    settings,
    state_store: TelegramStateStore,
    updates: queue.Queue[dict[str, Any] | None],
) -> ThreadingHTTPServer:
    webhook_secret = (
        settings.telegram.webhook_secret.get_secret_value()
        if settings.telegram.webhook_secret is not None
        else None
    )
    if webhook_secret is None or not webhook_secret.strip():
        raise RuntimeError(
            "TELEGRAM__WEBHOOK_SECRET must be configured for webhook mode."
        )

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != path:
                self._respond(HTTPStatus.NOT_FOUND, b"not found")
                return

            authentication = validate_webhook_secret(
                headers=dict(self.headers.items()),
                expected_secret=webhook_secret,
            )
            if not authentication.accepted:
                self._respond(HTTPStatus.FORBIDDEN, b"forbidden")
                return

            try:
                content_length = self._validated_content_length()
                self.connection.settimeout(WEBHOOK_READ_TIMEOUT_SECONDS)
                body = self.rfile.read(content_length)
                payload = decode_json_body(body)
            except TimeoutError:
                logger.info("Timed out reading Telegram webhook request")
                self._respond(HTTPStatus.REQUEST_TIMEOUT, b"request timeout")
                return
            except _RequestBodyError as exc:
                self._respond(exc.status, exc.body)
                return
            except Exception:
                logger.exception("Invalid Telegram webhook request")
                self._respond(HTTPStatus.BAD_REQUEST, b"invalid json")
                return

            validation = validate_webhook_update(
                payload=payload,
                headers=dict(self.headers.items()),
                expected_secret=webhook_secret,
                allowed_user_id=settings.telegram.allowed_user_id,
            )
            if not validation.accepted:
                status = (
                    HTTPStatus.FORBIDDEN
                    if validation.reason == "invalid_secret"
                    else HTTPStatus.OK
                )
                logger.info("Ignoring Telegram update: %s", validation.reason)
                self._respond(status, b"ignored")
                return

            update_id = payload["update_id"]
            if not state_store.record_update_once(update_id):
                logger.info("Ignoring duplicate Telegram update_id=%s", update_id)
                self._respond(HTTPStatus.OK, b"duplicate")
                return

            updates.put(payload)
            self._respond(HTTPStatus.OK, b"ok")

        def _validated_content_length(self) -> int:
            raw_length = self.headers.get("content-length")
            if raw_length is None:
                raise _RequestBodyError(
                    HTTPStatus.LENGTH_REQUIRED,
                    b"content length required",
                )
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise _RequestBodyError(
                    HTTPStatus.BAD_REQUEST,
                    b"invalid content length",
                ) from exc
            if content_length < 0:
                raise _RequestBodyError(
                    HTTPStatus.BAD_REQUEST,
                    b"invalid content length",
                )
            if content_length > MAX_WEBHOOK_BODY_BYTES:
                raise _RequestBodyError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    b"request too large",
                )
            return content_length

        def log_message(self, format: str, *args) -> None:
            logger.debug(format, *args)

        def _respond(self, status: HTTPStatus, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _TimeoutThreadingHTTPServer((host, port), WebhookHandler)


class _RequestBodyError(ValueError):
    def __init__(self, status: HTTPStatus, body: bytes):
        super().__init__(body.decode())
        self.status = status
        self.body = body


if __name__ == "__main__":
    main()

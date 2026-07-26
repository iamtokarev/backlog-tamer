from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.config import Settings, get_settings

from .handlers import (
    ALLOWED_USER_ID_KEY,
    INTAKE_SERVICE_KEY,
    TELEGRAM_STATE_STORE_KEY,
    handle_callback,
    handle_message,
)
from .state import TelegramStateStore

logger = logging.getLogger(__name__)


def build_application(
    settings: Settings,
    intake_service: IntakeService,
    state_store: TelegramStateStore | None = None,
) -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.telegram.bot_token.get_secret_value())
        .build()
    )
    application.bot_data[INTAKE_SERVICE_KEY] = intake_service
    application.bot_data[ALLOWED_USER_ID_KEY] = settings.telegram.allowed_user_id
    if settings.telegram.webhook_secret is not None:
        application.bot_data["telegram_webhook_secret"] = (
            settings.telegram.webhook_secret.get_secret_value()
        )
    application.bot_data[TELEGRAM_STATE_STORE_KEY] = state_store or TelegramStateStore(
        settings.database_url,
    )

    user_filter = filters.User(user_id=settings.telegram.allowed_user_id)

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & user_filter,
            handle_message,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_callback,
            pattern=r"^(approve|revise|reject|retry|edit|pick|back|cancel):",
        )
    )
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    intake_service = IntakeService(settings=settings)
    application = build_application(settings, intake_service)

    logger.info(
        "Starting Telegram bot for allowed_user_id=%s",
        settings.telegram.allowed_user_id,
    )
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()

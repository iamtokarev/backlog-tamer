from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationStatus, IntakeResult

from .parsing import build_incoming_context
from .rendering import (
    CALLBACK_APPROVE,
    CALLBACK_REJECT,
    CALLBACK_REVISE,
    REVISION_PROMPT,
    build_review_keyboard,
    render_draft_message,
    render_terminal_message,
)
from .state import (
    get_session_revision,
    set_session_revision,
    state_identity_from_update,
)

logger = logging.getLogger(__name__)

INTAKE_SERVICE_KEY = "intake_service"
ALLOWED_USER_ID_KEY = "allowed_user_id"
TELEGRAM_STATE_STORE_KEY = "telegram_state_store"
AWAITING_REVISION_KEY = "awaiting_revision_for"

ERROR_REPLY = "Something went wrong while triaging this item. Try again."
UNKNOWN_CONFIRMATION_REPLY = (
    "I lost track of that draft. Send the item again to start over."
)


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    identity = state_identity_from_update(update)
    if identity is None:
        return
    user_id, chat_id = identity
    pending_revision = get_session_revision(
        context,
        user_id=user_id,
        chat_id=chat_id,
    )
    if pending_revision is not None:
        await _handle_revision_text(update, context, pending_revision)
        return

    await _handle_new_intake(update, context)


async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    if not _is_allowed_user(update, context):
        await query.answer()
        return

    await query.answer()

    try:
        action, confirmation_id = query.data.split(":", 1)
    except ValueError:
        logger.warning("Malformed callback_data: %r", query.data)
        return

    intake_service = _get_intake_service(context)

    if action == CALLBACK_REVISE:
        identity = state_identity_from_update(update)
        if identity is None:
            return
        user_id, chat_id = identity
        set_session_revision(
            context,
            user_id=user_id,
            chat_id=chat_id,
            confirmation_id=confirmation_id,
        )
        await query.message.reply_text(
            REVISION_PROMPT,
            parse_mode=ParseMode.HTML,
        )
        return

    if action not in {CALLBACK_APPROVE, CALLBACK_REJECT}:
        logger.warning("Unknown callback action: %r", action)
        return

    try:
        result = await intake_service.resume_intake(confirmation_id, action)
    except ValueError:
        logger.exception("resume_intake failed for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected resume_intake error")
        await query.edit_message_text(ERROR_REPLY)
        return

    status = ConfirmationStatus(result.status)

    await query.edit_message_text(
        render_terminal_message(
            result.draft_proposal,
            status,
            result.notion_project_url,
        ),
        parse_mode=ParseMode.HTML,
    )


async def _handle_new_intake(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    intake_service = _get_intake_service(context)
    incoming = build_incoming_context(message)

    await chat.send_action(ChatAction.TYPING)

    try:
        result = await intake_service.start_intake(
            context=incoming,
            user_id=str(user.id),
            chat_id=str(chat.id),
            source_message_id=str(message.message_id),
        )
    except Exception:
        logger.exception("start_intake failed")
        await message.reply_text(ERROR_REPLY)
        return

    await _send_draft(update, result)


async def _handle_revision_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    confirmation_id: str,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    feedback = (message.text or "").strip()
    if not feedback:
        await message.reply_text(
            "Please send the revision feedback as text.",
        )
        identity = state_identity_from_update(update)
        if identity is not None:
            user_id, chat_id = identity
            set_session_revision(
                context,
                user_id=user_id,
                chat_id=chat_id,
                confirmation_id=confirmation_id,
            )
        return

    intake_service = _get_intake_service(context)

    await chat.send_action(ChatAction.TYPING)

    try:
        result = await intake_service.resume_intake(confirmation_id, feedback)
    except ValueError:
        logger.exception("resume_intake failed for confirmation %s", confirmation_id)
        await message.reply_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected resume_intake error during revision")
        await message.reply_text(ERROR_REPLY)
        return

    if result.status == "needs_review":
        await _send_draft(update, result)
        return

    status = ConfirmationStatus(result.status)
    await message.reply_text(
        render_terminal_message(
            result.draft_proposal,
            status,
            result.notion_project_url,
        ),
        parse_mode=ParseMode.HTML,
    )


async def _send_draft(update: Update, result: IntakeResult) -> None:
    message = update.effective_message
    if message is None or result.draft_proposal is None:
        return

    await message.reply_text(
        render_draft_message(result.draft_proposal),
        parse_mode=ParseMode.HTML,
        reply_markup=build_review_keyboard(result.confirmation_id),
    )


def _get_intake_service(context: ContextTypes.DEFAULT_TYPE) -> IntakeService:
    intake_service = context.bot_data.get(INTAKE_SERVICE_KEY)
    if intake_service is None:
        raise RuntimeError("IntakeService is not configured in bot_data.")
    return intake_service


def _is_allowed_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    allowed_user_id = context.bot_data.get(ALLOWED_USER_ID_KEY)
    user = update.effective_user
    if allowed_user_id is None or user is None:
        return True
    return user.id == allowed_user_id

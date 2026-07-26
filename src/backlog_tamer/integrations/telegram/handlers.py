from __future__ import annotations

import logging

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.agents.intake_triage.tools import fetch_url
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationStatus, IntakeResult

from .parsing import build_incoming_context
from .rendering import (
    CALLBACK_APPROVE,
    CALLBACK_BACK,
    CALLBACK_CANCEL,
    CALLBACK_EDIT,
    CALLBACK_PICK,
    CALLBACK_REFETCH,
    CALLBACK_REJECT,
    CALLBACK_RETRY,
    CALLBACK_REVISE,
    DRAFT_FIELD_NAMES,
    REVISION_PLACEHOLDER,
    build_cancel_revision_keyboard,
    build_picker_keyboard,
    build_retry_keyboard,
    build_review_keyboard,
    render_change_summary,
    render_draft_message,
    render_progress_message,
    render_revision_prompt,
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

DRAFT_ERROR_REPLY = (
    "I couldn't draft this one — the triage agent failed. Send it again to retry."
)
REVIEW_ERROR_REPLY = (
    "I couldn't apply that review step. The draft is still open — try again."
)
UNKNOWN_CONFIRMATION_REPLY = (
    "I lost track of that draft. Send the item again to start over."
)
REVISING_MESSAGE = "✍️ Revising the draft…"
SAVING_MESSAGE = "⏳ Saving to Notion…"
REFETCHING_MESSAGE = "🔎 Opening the page again…"
REFETCH_FEEDBACK = (
    "The page fetch failed last time. Fetch the source URL again and redraft "
    "from what the page actually says."
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

    try:
        action, confirmation_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        logger.warning("Malformed callback_data: %r", query.data)
        return

    # A callback query may only be answered once, so the toast goes here.
    await query.answer(
        text="Revision cancelled." if action == CALLBACK_CANCEL else None,
    )

    intake_service = _get_intake_service(context)

    if action in {CALLBACK_EDIT, CALLBACK_PICK, CALLBACK_BACK}:
        await _handle_quick_edit(query, intake_service, action, confirmation_id)
        return

    if action == CALLBACK_REVISE:
        identity = state_identity_from_update(update)
        if identity is None:
            return
        user_id, chat_id = identity
        draft = _require_draft(intake_service, confirmation_id)
        if draft is None:
            await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
            return
        set_session_revision(
            context,
            user_id=user_id,
            chat_id=chat_id,
            confirmation_id=confirmation_id,
        )
        # The card shows it is waiting, and offers the way out: without this
        # the next message sent for any reason is swallowed as feedback.
        await query.edit_message_reply_markup(
            reply_markup=build_cancel_revision_keyboard(confirmation_id),
        )
        await query.message.reply_text(
            render_revision_prompt(draft),
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=REVISION_PLACEHOLDER,
            ),
        )
        return

    if action == CALLBACK_CANCEL:
        identity = state_identity_from_update(update)
        if identity is not None:
            user_id, chat_id = identity
            get_session_revision(context, user_id=user_id, chat_id=chat_id)
        record = intake_service.store.get(confirmation_id)
        if record is None:
            await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
            return
        await query.edit_message_reply_markup(
            reply_markup=build_review_keyboard(
                record.draft_proposal,
                confirmation_id,
                record.grounding,
            ),
        )
        return

    if action == CALLBACK_REFETCH:
        await _handle_refetch(query, intake_service, confirmation_id)
        return

    if action not in {CALLBACK_APPROVE, CALLBACK_REJECT, CALLBACK_RETRY}:
        logger.warning("Unknown callback action: %r", action)
        return

    if action in {CALLBACK_APPROVE, CALLBACK_RETRY}:
        # Drops the keyboard too, so the button cannot be pressed twice.
        await query.edit_message_text(SAVING_MESSAGE, parse_mode=ParseMode.HTML)

    try:
        if action == CALLBACK_RETRY:
            result = await intake_service.finalize_approval(confirmation_id)
        else:
            result = await intake_service.resume_intake(confirmation_id, action)
    except ValueError:
        logger.exception("resume_intake failed for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected resume_intake error")
        await query.edit_message_text(REVIEW_ERROR_REPLY)
        return

    await query.edit_message_text(
        _terminal_text(result),
        parse_mode=ParseMode.HTML,
        reply_markup=_terminal_keyboard(result),
    )


async def _handle_refetch(
    query,
    intake_service: IntakeService,
    confirmation_id: str,
) -> None:
    """Ask the agent to open the page again after a failed fetch.

    fetch_url memoises results, so the cached failure has to go first or the
    retry would return it unchanged.
    """
    fetch_url.clear_cache()
    await query.edit_message_text(REFETCHING_MESSAGE, parse_mode=ParseMode.HTML)

    try:
        result = await intake_service.resume_intake(confirmation_id, REFETCH_FEEDBACK)
    except ValueError:
        logger.exception("refetch failed for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected refetch error")
        await query.edit_message_text(REVIEW_ERROR_REPLY)
        return

    if result.status != "needs_review":
        await query.edit_message_text(
            _terminal_text(result),
            parse_mode=ParseMode.HTML,
            reply_markup=_terminal_keyboard(result),
        )
        return

    await _show_draft(query.message, result)


async def _handle_quick_edit(
    query,
    intake_service: IntakeService,
    action: str,
    payload: str,
) -> None:
    """Open a field picker, apply a pick, or go back — all without the agent."""
    if action == CALLBACK_EDIT:
        field_code, confirmation_id = payload.split(":", 1)
        if field_code not in DRAFT_FIELD_NAMES:
            logger.warning("Unknown quick-edit field: %r", field_code)
            return
        await query.edit_message_reply_markup(
            reply_markup=build_picker_keyboard(field_code, confirmation_id),
        )
        return

    if action == CALLBACK_BACK:
        confirmation_id = payload
        record = intake_service.store.get(confirmation_id)
        if record is None:
            await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
            return
        await query.edit_message_reply_markup(
            reply_markup=build_review_keyboard(
                record.draft_proposal,
                confirmation_id,
                record.grounding,
            ),
        )
        return

    field_code, value, confirmation_id = payload.split(":", 2)
    if field_code not in DRAFT_FIELD_NAMES:
        logger.warning("Unknown quick-edit field: %r", field_code)
        return

    try:
        record = intake_service.store.apply_manual_edit(
            confirmation_id=confirmation_id,
            field=DRAFT_FIELD_NAMES[field_code],
            value=value,
        )
    except ValueError:
        logger.warning("Quick edit rejected for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return

    await query.edit_message_text(
        render_draft_message(record.draft_proposal, record.grounding),
        parse_mode=ParseMode.HTML,
        reply_markup=build_review_keyboard(
            record.draft_proposal,
            confirmation_id,
            record.grounding,
        ),
    )


def _require_draft(intake_service: IntakeService, confirmation_id: str):
    record = intake_service.store.get(confirmation_id)
    return record.draft_proposal if record is not None else None


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

    progress = await message.reply_text(
        render_progress_message(incoming),
        parse_mode=ParseMode.HTML,
    )

    try:
        result = await intake_service.start_intake(
            context=incoming,
            user_id=str(user.id),
            chat_id=str(chat.id),
            source_message_id=str(message.message_id),
        )
    except Exception:
        logger.exception("start_intake failed")
        await progress.edit_text(DRAFT_ERROR_REPLY)
        return

    await _show_draft(progress, result)


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
    before = _require_draft(intake_service, confirmation_id)

    progress = await message.reply_text(REVISING_MESSAGE, parse_mode=ParseMode.HTML)

    try:
        result = await intake_service.resume_intake(confirmation_id, feedback)
    except ValueError:
        logger.exception("resume_intake failed for confirmation %s", confirmation_id)
        await progress.edit_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected resume_intake error during revision")
        await progress.edit_text(REVIEW_ERROR_REPLY)
        return

    if result.status == "needs_review":
        await _show_draft(progress, result, before=before)
        return

    await progress.edit_text(
        _terminal_text(result),
        parse_mode=ParseMode.HTML,
        reply_markup=_terminal_keyboard(result),
    )


async def _show_draft(
    progress_message,
    result: IntakeResult,
    before: ProjectDraft | None = None,
) -> None:
    """Turn the progress placeholder into the review card."""
    if result.draft_proposal is None:
        return

    card = render_draft_message(result.draft_proposal, result.grounding)
    if before is not None:
        changes = render_change_summary(before, result.draft_proposal)
        if changes is not None:
            card = f"{changes}\n\n{card}"

    await progress_message.edit_text(
        card,
        parse_mode=ParseMode.HTML,
        reply_markup=build_review_keyboard(
            result.draft_proposal,
            result.confirmation_id,
            result.grounding,
        ),
    )


def _terminal_text(result: IntakeResult) -> str:
    return render_terminal_message(
        result.draft_proposal,
        ConfirmationStatus(result.status),
        result.notion_project_url,
        result.failure_reason,
    )


def _terminal_keyboard(result: IntakeResult):
    """A failed Notion write keeps the draft, so offer the write again."""
    if ConfirmationStatus(result.status) is not ConfirmationStatus.FAILED:
        return None
    return build_retry_keyboard(result.confirmation_id)


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

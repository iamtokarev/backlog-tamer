from __future__ import annotations

import logging

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationStatus, IntakeResult

from .callbacks import (
    Action,
    ConfirmationAction,
    OpenPicker,
    PickValue,
    decode_callback,
)
from .parsing import build_incoming_context
from .rendering import (
    REVISION_PLACEHOLDER,
    build_cancel_revision_keyboard,
    build_picker_keyboard,
    build_review_keyboard,
    build_terminal_keyboard,
    render_change_summary,
    render_draft_message,
    render_progress_message,
    render_revision_prompt,
    render_terminal_message,
)
from .state import (
    PendingRevision,
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
UNDOING_MESSAGE = "↩️ Removing it from Notion…"
ADDING_TASK_MESSAGE = "＋ Adding the task to the existing project…"
REFETCHING_MESSAGE = "🔎 Opening the page again…"
SUPERSEDED_NOTE = "⤵️ <i>Revised below.</i>"


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

    callback = decode_callback(query.data)
    if callback is None:
        await query.answer()
        logger.warning("Malformed callback_data: %r", query.data)
        return

    # A callback query may only be answered once, so the toast goes here.
    cancelled = (
        isinstance(callback, ConfirmationAction) and callback.action is Action.CANCEL
    )
    await query.answer(text="Revision cancelled." if cancelled else None)

    intake_service = _get_intake_service(context)
    confirmation_id = callback.confirmation_id

    if isinstance(callback, OpenPicker):
        await query.edit_message_reply_markup(
            reply_markup=build_picker_keyboard(callback.field, confirmation_id),
        )
        return

    if isinstance(callback, PickValue):
        await _apply_quick_edit(query, intake_service, callback)
        return

    action = callback.action

    if action == Action.BACK:
        await _restore_review_keyboard(query, intake_service, confirmation_id)
        return

    if action == Action.REVISE:
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
            card_message_id=query.message.message_id,
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

    if action == Action.CANCEL:
        identity = state_identity_from_update(update)
        if identity is not None:
            user_id, chat_id = identity
            get_session_revision(context, user_id=user_id, chat_id=chat_id)
        await _restore_review_keyboard(query, intake_service, confirmation_id)
        return

    if action == Action.REFETCH:
        await _handle_refetch(query, intake_service, confirmation_id)
        return

    # Each of these edits the card first, which also drops the keyboard so the
    # button cannot be pressed twice while the write is in flight.
    pending_message = {
        Action.APPROVE: SAVING_MESSAGE,
        Action.RETRY: SAVING_MESSAGE,
        Action.UNDO: UNDOING_MESSAGE,
        Action.ADD_TASK: ADDING_TASK_MESSAGE,
    }.get(action)
    if pending_message is not None:
        await query.edit_message_text(pending_message, parse_mode=ParseMode.HTML)

    try:
        if action == Action.RETRY:
            result = await intake_service.finalize_approval(confirmation_id)
        elif action == Action.UNDO:
            result = await intake_service.undo_commit(confirmation_id)
        elif action == Action.ADD_TASK:
            result = await intake_service.add_to_existing_project(confirmation_id)
        elif action == Action.APPROVE:
            result = await intake_service.approve(confirmation_id)
        else:
            result = await intake_service.reject(confirmation_id)
    except ValueError:
        logger.exception("review action failed for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected review action error")
        await _show_review_error(
            query.edit_message_text, intake_service, confirmation_id
        )
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
    """Ask the agent to open the page again after a failed fetch."""
    await query.edit_message_text(REFETCHING_MESSAGE, parse_mode=ParseMode.HTML)

    try:
        result = await intake_service.refetch(confirmation_id)
    except ValueError:
        logger.exception("refetch failed for confirmation %s", confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected refetch error")
        await _show_review_error(
            query.edit_message_text, intake_service, confirmation_id
        )
        return

    if result.status != "needs_review":
        await query.edit_message_text(
            _terminal_text(result),
            parse_mode=ParseMode.HTML,
            reply_markup=_terminal_keyboard(result),
        )
        return

    await _show_draft(query.message, result)


async def _apply_quick_edit(
    query,
    intake_service: IntakeService,
    pick: PickValue,
) -> None:
    """Set the picked value on the Draft without asking the agent."""
    try:
        review = intake_service.apply_quick_edit(
            pick.confirmation_id,
            pick.field,
            pick.value,
        )
    except ValueError:
        logger.warning("Quick edit rejected for confirmation %s", pick.confirmation_id)
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return

    await query.edit_message_text(
        render_draft_message(review.draft_proposal, review.grounding),
        parse_mode=ParseMode.HTML,
        reply_markup=_review_keyboard(review),
    )


async def _show_review_error(
    edit,
    intake_service: IntakeService,
    confirmation_id: str,
) -> None:
    """Say the step failed, with the Draft and its keyboard while it is open.

    The failed step already replaced the card (and its keyboard), so without
    the keyboard back there would be nothing left to try again with.
    """
    try:
        review = intake_service.get_review(confirmation_id)
    except Exception:
        logger.exception("Could not reload confirmation %s", confirmation_id)
        review = None
    if review is None or review.status != "needs_review":
        await edit(REVIEW_ERROR_REPLY)
        return

    card = render_draft_message(review.draft_proposal, review.grounding)
    await edit(
        f"{REVIEW_ERROR_REPLY}\n\n{card}",
        parse_mode=ParseMode.HTML,
        reply_markup=_review_keyboard(review),
    )


async def _restore_review_keyboard(
    query,
    intake_service: IntakeService,
    confirmation_id: str,
) -> None:
    review = intake_service.get_review(confirmation_id)
    if review is None:
        await query.edit_message_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    await query.edit_message_reply_markup(
        reply_markup=_review_keyboard(review),
    )


def _require_draft(intake_service: IntakeService, confirmation_id: str):
    review = intake_service.get_review(confirmation_id)
    return review.draft_proposal if review is not None else None


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
    pending: PendingRevision,
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
                confirmation_id=pending.confirmation_id,
                card_message_id=pending.card_message_id,
            )
        return

    confirmation_id = pending.confirmation_id
    intake_service = _get_intake_service(context)
    review = intake_service.get_review(confirmation_id)
    before = review.draft_proposal if review is not None else None
    if review is not None and pending.card_message_id is not None:
        await _retire_card(context, chat.id, pending.card_message_id, review)

    progress = await message.reply_text(REVISING_MESSAGE, parse_mode=ParseMode.HTML)

    try:
        result = await intake_service.revise(confirmation_id, feedback)
    except ValueError:
        logger.exception("revision failed for confirmation %s", confirmation_id)
        await progress.edit_text(UNKNOWN_CONFIRMATION_REPLY)
        return
    except Exception:
        logger.exception("Unexpected error during revision")
        await _show_review_error(progress.edit_text, intake_service, confirmation_id)
        return

    if result.status == "needs_review":
        await _show_draft(progress, result, before=before)
        return

    await progress.edit_text(
        _terminal_text(result),
        parse_mode=ParseMode.HTML,
        reply_markup=_terminal_keyboard(result),
    )


async def _retire_card(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    review: IntakeResult,
) -> None:
    """Mark the card the Revision started from as replaced, and drop its keyboard.

    The Revision answers in a new message, which becomes the live card; left
    alone, the old one would keep offering "Cancel revision" for a Revision
    that is already under way.
    """
    card = render_draft_message(review.draft_proposal, review.grounding)
    try:
        await context.bot.edit_message_text(
            f"{SUPERSEDED_NOTE}\n\n{card}",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
    except TelegramError:
        # Cosmetic: the Revision itself must still run.
        logger.warning("Could not retire review card %s", message_id)


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
        reply_markup=_review_keyboard(result),
    )


def _review_keyboard(review: IntakeResult):
    return build_review_keyboard(
        review.draft_proposal,
        review.confirmation_id,
        review.grounding,
    )


def _terminal_text(result: IntakeResult) -> str:
    return render_terminal_message(
        result.draft_proposal,
        ConfirmationStatus(result.status),
        result.notion_project_url,
        result.failure_reason,
        result.duplicate_created_time,
    )


def _terminal_keyboard(result: IntakeResult):
    return build_terminal_keyboard(
        ConfirmationStatus(result.status),
        result.confirmation_id,
        result.notion_project_url,
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

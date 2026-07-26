from __future__ import annotations

from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"

REVISION_PROMPT = "✏️ Send your revision as a reply."


def render_draft_message(draft: ProjectDraft) -> str:
    tasks = "\n".join(f"• {escape(task)}" for task in draft.tasks)

    lines = [
        f"<b>Project:</b> {escape(draft.project_name)}",
        f"<b>Type:</b> {escape(draft.resource_type)}",
        f"<b>Intent:</b> {escape(draft.intent)}",
        f"<b>Priority:</b> {escape(draft.priority)}",
    ]
    if draft.source_url:
        lines.append(f"<b>Source:</b> {_link(draft.source_url, draft.source_url)}")

    lines.extend(
        [
            "",
            "<b>Summary:</b>",
            escape(draft.summary),
            "",
            "<b>Tasks:</b>",
            tasks,
        ]
    )
    return "\n".join(lines)


def build_review_keyboard(confirmation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"{CALLBACK_APPROVE}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    "📝 Revise",
                    callback_data=f"{CALLBACK_REVISE}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"{CALLBACK_REJECT}:{confirmation_id}",
                ),
            ]
        ]
    )


def render_terminal_message(
    draft: ProjectDraft,
    status: ConfirmationStatus,
    notion_project_url: str | None = None,
) -> str:
    body = render_draft_message(draft)
    badge = _terminal_badge(status)
    if notion_project_url:
        link = _link(notion_project_url, notion_project_url)
        return f"{badge}\n<b>Notion:</b> {link}\n\n{body}"
    return f"{badge}\n\n{body}"


def _terminal_badge(status: ConfirmationStatus) -> str:
    if status is ConfirmationStatus.COMMITTED:
        return "✅ <b>Saved</b>"
    if status is ConfirmationStatus.COMMITTING:
        return "⏳ <b>Saving</b>"
    if status is ConfirmationStatus.FAILED:
        return "⚠️ <b>Save failed</b>"
    if status is ConfirmationStatus.REJECTED:
        return "❌ <b>Rejected</b>"
    return "⏳ <b>Pending</b>"


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'

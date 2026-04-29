from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"

REVISION_PROMPT = "✏️ Send your revision as a reply\\."


def render_draft_message(draft: ProjectDraft) -> str:
    project_name = escape_markdown(draft.project_name, version=2)
    summary = escape_markdown(draft.summary, version=2)
    resource_type = escape_markdown(draft.resource_type, version=2)
    intent = escape_markdown(draft.intent, version=2)
    priority = escape_markdown(draft.priority, version=2)
    tasks = "\n".join(f"• {escape_markdown(task, version=2)}" for task in draft.tasks)

    lines = [
        f"*Project:* {project_name}",
        f"*Type:* {resource_type}",
        f"*Intent:* {intent}",
        f"*Priority:* {priority}",
    ]
    if draft.source_url:
        url_label = escape_markdown(draft.source_url, version=2)
        url_target = _escape_link_target(draft.source_url)
        lines.append(f"*Source:* [{url_label}]({url_target})")

    lines.extend(
        [
            "",
            "*Summary:*",
            summary,
            "",
            "*Tasks:*",
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
        url_label = escape_markdown(notion_project_url, version=2)
        url_target = _escape_link_target(notion_project_url)
        return f"{badge}\n*Notion:* [{url_label}]({url_target})\n\n{body}"
    return f"{badge}\n\n{body}"


def _terminal_badge(status: ConfirmationStatus) -> str:
    if status is ConfirmationStatus.COMMITTED:
        return "✅ *Saved*"
    if status is ConfirmationStatus.COMMITTING:
        return "⏳ *Saving*"
    if status is ConfirmationStatus.FAILED:
        return "⚠️ *Save failed*"
    if status is ConfirmationStatus.REJECTED:
        return "❌ *Rejected*"
    return "⏳ *Pending*"


def _escape_link_target(url: str) -> str:
    return url.replace("\\", "\\\\").replace(")", "\\)")

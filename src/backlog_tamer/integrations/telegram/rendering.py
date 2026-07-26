from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"

REVISION_PROMPT = "✏️ Send your revision as a reply."

RESOURCE_TYPE_ICONS = {
    "article": "📄",
    "paper": "🧪",
    "video": "🎬",
    "course": "🎓",
    "documentation": "📘",
    "repository": "📦",
    "idea": "💡",
    "unknown": "❔",
}

INTENT_ICONS = {
    "learn": "📚",
    "build": "🔨",
    "research": "🔬",
    "explore": "🧭",
    "reference": "🔖",
    "unclear": "❔",
}

PRIORITY_ICONS = {
    "High": "🔺",
    "Medium": "▪️",
    "Low": "🔻",
}


def render_draft_message(draft: ProjectDraft) -> str:
    """Render the review card: title first, then meta, summary, source, tasks."""
    lines = [
        f"<b>{escape(draft.project_name)}</b>",
        _render_chips(draft),
        "",
        escape(draft.summary),
    ]

    if draft.source_url:
        lines.append("")
        lines.append(f"🔗 {_link(draft.source_url, _display_url(draft.source_url))}")

    if draft.tasks:
        lines.append("")
        lines.extend(f"☑︎ {escape(task)}" for task in draft.tasks)

    return "\n".join(lines)


def _render_chips(draft: ProjectDraft) -> str:
    chips = [
        f"{RESOURCE_TYPE_ICONS.get(draft.resource_type, '❔')} {draft.resource_type}",
        f"{INTENT_ICONS.get(draft.intent, '❔')} {draft.intent}",
        f"{PRIORITY_ICONS.get(draft.priority, '▪️')} {draft.priority}",
    ]
    return escape(" · ".join(chips))


def _display_url(url: str) -> str:
    """Show the domain rather than a URL that wraps three times on a phone."""
    host = urlparse(url).netloc
    if not host:
        return url
    return host.removeprefix("www.")


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
